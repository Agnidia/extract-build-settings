#!/usr/bin/env python3
"""
Extract Build Settings from xcodeproj to xcconfig files.

This script:
1. Parses a project.pbxproj file
2. Extracts build settings for all targets
3. Generates organized xcconfig files
4. Updates pbxproj to reference xcconfig files
5. Removes inline build settings from pbxproj

Usage:
    python extract_build_settings.py --project MyApp.xcodeproj [--dry-run] [--extract-only] [--rollback]
"""

import os
import re
import shutil
import argparse
import random
from datetime import datetime
from pathlib import Path
from collections import defaultdict
from typing import NamedTuple


# Signing-related build settings to extract to sign.xcconfig
SIGNING_KEYS = {
    "DEVELOPMENT_TEAM",
    "CODE_SIGN_STYLE",
    "CODE_SIGN_IDENTITY",
    "PROVISIONING_PROFILE_SPECIFIER",
    "PROVISIONING_PROFILE",
    "CODE_SIGN_INJECT_BASE_ENTITLEMENTS",
}

# Build settings categories (matching Xcode's Build Settings groups)
# Order matters for output - categories will appear in this order
SETTING_CATEGORIES = [
    ("Architectures", {
        "ARCHS", "VALID_ARCHS", "ONLY_ACTIVE_ARCH", "SUPPORTED_PLATFORMS",
        "TARGETED_DEVICE_FAMILY", "SDKROOT", "ALLOW_TARGET_PLATFORM_SPECIALIZATION",
        "SUPPORTS_MACCATALYST",
    }),
    ("Build Options", {
        "DEBUG_INFORMATION_FORMAT", "ENABLE_BITCODE", "ENABLE_TESTABILITY",
        "ENABLE_PREVIEWS", "ENABLE_STRICT_OBJC_MSGSEND", "ENABLE_USER_SCRIPT_SANDBOXING",
        "ENABLE_NS_ASSERTIONS", "VALIDATE_PRODUCT", "COPY_PHASE_STRIP",
    }),
    ("Deployment", {
        "IPHONEOS_DEPLOYMENT_TARGET", "MACOSX_DEPLOYMENT_TARGET",
        "TVOS_DEPLOYMENT_TARGET", "WATCHOS_DEPLOYMENT_TARGET", "XROS_DEPLOYMENT_TARGET",
        "SKIP_INSTALL", "TARGETED_DEVICE_FAMILY",
    }),
    ("Signing", {
        "DEVELOPMENT_TEAM", "CODE_SIGN_STYLE", "CODE_SIGN_IDENTITY",
        "CODE_SIGN_ENTITLEMENTS", "PROVISIONING_PROFILE_SPECIFIER",
        "PROVISIONING_PROFILE", "CODE_SIGN_INJECT_BASE_ENTITLEMENTS",
    }),
    ("Packaging", {
        "PRODUCT_NAME", "PRODUCT_BUNDLE_IDENTIFIER", "PRODUCT_MODULE_NAME",
        "INFOPLIST_FILE", "GENERATE_INFOPLIST_FILE", "MARKETING_VERSION",
        "CURRENT_PROJECT_VERSION", "VERSIONING_SYSTEM", "DEFINES_MODULE",
        "REGISTER_APP_GROUPS",
    }),
    ("Info.plist Values", set()),  # Will match INFOPLIST_KEY_* prefix
    ("Linking", {
        "LD_RUNPATH_SEARCH_PATHS", "DEAD_CODE_STRIPPING", "OTHER_LDFLAGS",
        "MACH_O_TYPE", "DYLIB_COMPATIBILITY_VERSION", "DYLIB_CURRENT_VERSION",
    }),
    ("Search Paths", {
        "ALWAYS_SEARCH_USER_PATHS", "FRAMEWORK_SEARCH_PATHS", "HEADER_SEARCH_PATHS",
        "LIBRARY_SEARCH_PATHS", "USER_HEADER_SEARCH_PATHS",
    }),
    ("Apple Clang - Code Generation", {
        "GCC_DYNAMIC_NO_PIC", "GCC_NO_COMMON_BLOCKS", "GCC_OPTIMIZATION_LEVEL",
        "GCC_PREPROCESSOR_DEFINITIONS",
    }),
    ("Apple Clang - Language", {
        "GCC_C_LANGUAGE_STANDARD", "CLANG_CXX_LANGUAGE_STANDARD",
        "CLANG_ENABLE_MODULES", "CLANG_ENABLE_OBJC_ARC", "CLANG_ENABLE_OBJC_WEAK",
    }),
    ("Apple Clang - Warnings", set()),  # Will match CLANG_WARN_* and GCC_WARN_* prefixes
    ("Apple Clang - Analyzer", set()),  # Will match CLANG_ANALYZER_* prefix
    ("Swift Compiler", set()),  # Will match SWIFT_* prefix
    ("Asset Catalog Compiler", set()),  # Will match ASSETCATALOG_* prefix
    ("Localization", {
        "LOCALIZATION_PREFERS_STRING_CATALOGS", "STRING_CATALOG_GENERATE_SYMBOLS",
    }),
    ("Metal Compiler", set()),  # Will match MTL_* prefix
    ("User-Defined", set()),  # Catch-all for custom settings
]


def get_setting_category(key: str) -> str:
    """Get the category for a build setting key."""
    # Strip platform-specific suffix for matching
    base_key = key.split("[")[0]

    # Check prefix-based categories first
    if base_key.startswith("INFOPLIST_KEY_"):
        return "Info.plist Values"
    if base_key.startswith("CLANG_WARN_") or base_key.startswith("GCC_WARN_"):
        return "Apple Clang - Warnings"
    if base_key.startswith("CLANG_ANALYZER_"):
        return "Apple Clang - Analyzer"
    if base_key.startswith("SWIFT_"):
        return "Swift Compiler"
    if base_key.startswith("ASSETCATALOG_"):
        return "Asset Catalog Compiler"
    if base_key.startswith("MTL_"):
        return "Metal Compiler"

    # Check exact match categories
    for category_name, keys in SETTING_CATEGORIES:
        if base_key in keys:
            return category_name

    # Default to User-Defined for unrecognized settings
    return "User-Defined"


def group_settings_by_category(settings: dict) -> dict[str, dict]:
    """Group settings by their category.

    Returns:
        Dictionary mapping category name to settings dict, in order.
    """
    # Initialize with empty dicts to maintain category order
    grouped = {name: {} for name, _ in SETTING_CATEGORIES}

    for key, value in settings.items():
        category = get_setting_category(key)
        grouped[category][key] = value

    # Remove empty categories
    return {cat: items for cat, items in grouped.items() if items}


def is_signing_key(key: str) -> bool:
    """Check if a key is signing-related (including platform-specific variants)."""
    # Handle platform-specific keys like CODE_SIGN_IDENTITY[sdk=iphoneos*]
    base_key = key.split("[")[0]
    return base_key in SIGNING_KEYS


def separate_signing_settings(settings: dict) -> tuple[dict, dict]:
    """Separate signing settings from other settings.

    Returns:
        (signing_settings, other_settings)
    """
    signing = {}
    other = {}

    for key, value in settings.items():
        if is_signing_key(key):
            signing[key] = value
        else:
            other[key] = value

    return signing, other


class BuildConfig(NamedTuple):
    """Represents a build configuration with its UUID and settings."""
    uuid: str
    name: str  # Debug or Release
    target: str  # PBXProject or target name
    settings: dict


class ProjectContext:
    """Holds all project-specific paths and metadata."""
    def __init__(self, xcodeproj_path: Path):
        self.xcodeproj_path = xcodeproj_path
        self.project_dir = xcodeproj_path.parent
        self.pbxproj_path = xcodeproj_path / "project.pbxproj"
        self.configurations_dir = self.project_dir / "Configurations"
        self.backup_path = self.pbxproj_path.with_suffix(".pbxproj.backup")
        self.project_name = xcodeproj_path.stem  # e.g., "MyApp" from "MyApp.xcodeproj"


def parse_pbxproj(content: str) -> dict:
    """Parse pbxproj content and extract build configurations."""
    result = {
        "project_name": None,
        "project_debug": None,
        "project_release": None,
        "targets": defaultdict(dict),  # target_name -> {debug: config, release: config}
        "configuration_lists": {},  # uuid -> list info
        "main_group_uuid": None,
        "root_object_uuid": None,
    }

    # Extract root object UUID
    root_match = re.search(r'rootObject\s*=\s*([A-F0-9]+)', content)
    if root_match:
        result["root_object_uuid"] = root_match.group(1)

    # Find main group UUID - look for the root group (one without explicit parent)
    # This is the group that has children but isn't referenced as a child by other groups
    main_group_pattern = re.compile(
        r'([A-F0-9]+)\s*=\s*\{\s*isa\s*=\s*PBXGroup;\s*children\s*=\s*\([^)]+\);\s*sourceTree\s*=\s*"<group>";\s*\}',
        re.MULTILINE | re.DOTALL
    )
    for match in main_group_pattern.finditer(content):
        # The main group typically doesn't have a 'path' attribute
        group_uuid = match.group(1)
        group_block = match.group(0)
        if 'path' not in group_block:
            result["main_group_uuid"] = group_uuid
            break

    # Find XCBuildConfiguration sections
    config_pattern = re.compile(
        r'([A-F0-9]+)\s*/\*\s*(Debug|Release)\s+configuration\s+for\s+(PBXProject|PBXNativeTarget)\s+"([^"]+)"\s*\*/\s*=\s*\{[^}]*buildSettings\s*=\s*\{([^}]+)\}',
        re.MULTILINE | re.DOTALL
    )

    for match in config_pattern.finditer(content):
        uuid = match.group(1)
        config_name = match.group(2)  # Debug or Release
        config_type = match.group(3)  # PBXProject or PBXNativeTarget
        target_name = match.group(4)
        settings_block = match.group(5)

        settings = parse_settings_block(settings_block)
        config = BuildConfig(uuid=uuid, name=config_name, target=target_name, settings=settings)

        if config_type == "PBXProject":
            result["project_name"] = target_name
            if config_name == "Debug":
                result["project_debug"] = config
            else:
                result["project_release"] = config
        else:
            result["targets"][target_name][config_name.lower()] = config

    # Find XCConfigurationList sections
    config_list_pattern = re.compile(
        r'([A-F0-9]+)\s*/\*\s*Build configuration list for (PBXProject|PBXNativeTarget)\s+"([^"]+)"\s*\*/\s*=\s*\{',
        re.MULTILINE
    )
    for match in config_list_pattern.finditer(content):
        uuid = match.group(1)
        list_type = match.group(2)
        target_name = match.group(3)
        result["configuration_lists"][uuid] = {
            "type": list_type,
            "target": target_name
        }

    return result


def parse_settings_block(block: str) -> dict:
    """Parse a buildSettings block into a dictionary."""
    settings = {}

    lines = block.strip().split('\n')
    i = 0
    while i < len(lines):
        line = lines[i].strip()

        # Skip empty lines
        if not line:
            i += 1
            continue

        # Handle array values that span multiple lines
        if '=' in line and '(' in line and ')' not in line:
            full_line = line
            i += 1
            while i < len(lines):
                next_line = lines[i].strip()
                full_line += ' ' + next_line
                if next_line.endswith(');') or next_line == ');':
                    break
                i += 1
            line = full_line

        # Parse the setting
        match = re.match(r'^\s*"([^"]+)"\s*=\s*(.+)', line)  # Quoted key
        if not match:
            match = re.match(r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+)', line)  # Unquoted key
        if match:
            key = match.group(1).strip()
            value = match.group(2).strip()

            # Remove trailing semicolon
            if value.endswith(';'):
                value = value[:-1].strip()

            # Handle array values
            if value.startswith('('):
                paren_end = value.rfind(')')
                if paren_end != -1:
                    array_content = value[1:paren_end].strip()
                    items = []
                    for item in re.findall(r'"([^"]+)"|([^,\s]+)', array_content):
                        val = item[0] or item[1]
                        if val and val != ',':
                            items.append(val.strip())
                    settings[key] = items
                else:
                    settings[key] = value
            else:
                if value.startswith('"') and value.endswith('"'):
                    value = value[1:-1]
                settings[key] = value

        i += 1

    return settings


def find_common_settings(debug_settings: dict, release_settings: dict) -> tuple[dict, dict, dict]:
    """Find settings common to both Debug and Release configurations."""
    common = {}
    debug_only = {}
    release_only = {}

    all_keys = set(debug_settings.keys()) | set(release_settings.keys())

    for key in all_keys:
        debug_val = debug_settings.get(key)
        release_val = release_settings.get(key)

        if debug_val == release_val and debug_val is not None:
            common[key] = debug_val
        else:
            if debug_val is not None:
                debug_only[key] = debug_val
            if release_val is not None:
                release_only[key] = release_val

    return common, debug_only, release_only


def format_xcconfig_value(value) -> str:
    """Format a value for xcconfig file."""
    if isinstance(value, list):
        return ' '.join(str(v) for v in value)
    return str(value)


def write_xcconfig(path: Path, settings: dict, project_name: str,
                   includes: list[str] = None, header_comment: str = None,
                   dry_run: bool = False, group_by_category: bool = True):
    """Write settings to an xcconfig file.

    Args:
        path: Output file path
        settings: Dictionary of build settings
        project_name: Project name for header
        includes: List of xcconfig files to include
        header_comment: Comment to add after header
        dry_run: If True, only print what would be written
        group_by_category: If True, group settings by Xcode category
    """
    lines = []

    # Header
    lines.append("//")
    lines.append(f"//  {path.name}")
    lines.append(f"//  {project_name}")
    lines.append("//")
    lines.append(f"//  Generated on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("//")
    lines.append("")
    lines.append("// Configuration settings file format documentation can be found at:")
    lines.append("// https://help.apple.com/xcode/#/dev745c5c974")
    lines.append("")

    if header_comment:
        lines.append(f"// {header_comment}")
        lines.append("")

    # Includes
    if includes:
        for inc in includes:
            lines.append(f'#include "{inc}"')
        lines.append("")

    # Settings
    if settings:
        if group_by_category and len(settings) > 5:
            # Group settings by category for better organization
            grouped = group_settings_by_category(settings)
            first_category = True
            for category, category_settings in grouped.items():
                if category_settings:
                    if not first_category:
                        lines.append("")
                    lines.append(f"// MARK: - {category}")
                    for key in sorted(category_settings.keys()):
                        value = category_settings[key]
                        formatted_value = format_xcconfig_value(value)
                        lines.append(f"{key} = {formatted_value}")
                    first_category = False
        else:
            # Simple alphabetical sorting for small files
            sorted_settings = sorted(settings.items())
            for key, value in sorted_settings:
                formatted_value = format_xcconfig_value(value)
                lines.append(f"{key} = {formatted_value}")

    lines.append("")

    content = '\n'.join(lines)

    if dry_run:
        print(f"\n[DRY RUN] Would write to {path}:")
        print("-" * 60)
        print(content[:800] + ("..." if len(content) > 800 else ""))
        print("-" * 60)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        print(f"  Created: {path}")


def generate_target_info(parsed: dict) -> dict:
    """Generate target info mapping dynamically from parsed data."""
    target_info = {}
    project_name = parsed["project_name"]

    for target_name in parsed["targets"].keys():
        # Normalize target name for folder/prefix
        normalized = target_name.lower().replace("-", "").replace("_", "").replace(" ", "")

        # Detect target type and assign appropriate folder/prefix
        if target_name == project_name:
            # Main app target
            target_info[target_name] = {"folder": "app", "prefix": "app", "type": "app"}
        elif "test" in normalized and "ui" in normalized:
            target_info[target_name] = {"folder": "uitests", "prefix": "uitest", "type": "uitest"}
        elif "test" in normalized:
            target_info[target_name] = {"folder": "tests", "prefix": "test", "type": "test"}
        else:
            # Extension or other target - use sanitized name
            safe_name = re.sub(r'[^a-z0-9]', '', normalized)
            target_info[target_name] = {"folder": safe_name, "prefix": safe_name, "type": "extension"}

    return target_info


def generate_xcconfig_files(ctx: ProjectContext, parsed: dict, dry_run: bool = False):
    """Generate all xcconfig files from parsed build configurations."""
    print("\n=== Generating xcconfig files ===")

    project_name = parsed["project_name"]
    project_debug = parsed["project_debug"]
    project_release = parsed["project_release"]

    # Generate target info dynamically
    target_info = generate_target_info(parsed)

    base_settings, debug_only, release_only = find_common_settings(
        project_debug.settings, project_release.settings
    )

    # Separate signing settings from base settings
    signing_settings, base_settings_no_sign = separate_signing_settings(base_settings)

    # Write sign.xcconfig (signing-specific settings)
    write_xcconfig(
        ctx.configurations_dir / "sign.xcconfig",
        signing_settings,
        project_name,
        header_comment="Code signing settings (can be gitignored for personal signing)",
        dry_run=dry_run
    )

    # Write base.xcconfig (includes sign.xcconfig)
    write_xcconfig(
        ctx.configurations_dir / "base.xcconfig",
        base_settings_no_sign,
        project_name,
        includes=["sign.xcconfig"],
        header_comment="Shared settings for all configurations",
        dry_run=dry_run
    )

    # Write debug.xcconfig
    write_xcconfig(
        ctx.configurations_dir / "debug.xcconfig",
        debug_only,
        project_name,
        includes=["base.xcconfig"],
        header_comment="Debug-specific settings",
        dry_run=dry_run
    )

    # Write release.xcconfig
    write_xcconfig(
        ctx.configurations_dir / "release.xcconfig",
        release_only,
        project_name,
        includes=["base.xcconfig"],
        header_comment="Release-specific settings",
        dry_run=dry_run
    )

    # Generate target-specific configs
    for target_name, configs in parsed["targets"].items():
        info = target_info.get(target_name)
        if not info:
            print(f"  Warning: Could not determine info for target '{target_name}', skipping")
            continue

        folder = info["folder"]
        prefix = info["prefix"]

        debug_config = configs.get("debug")
        release_config = configs.get("release")

        if not debug_config or not release_config:
            print(f"  Warning: Missing configuration for target '{target_name}'")
            continue

        target_common, target_debug, target_release = find_common_settings(
            debug_config.settings, release_config.settings
        )

        # Separate signing settings from target common settings
        target_signing, target_common_no_sign = separate_signing_settings(target_common)

        # Write target base config (includes sign.xcconfig for signing inheritance)
        write_xcconfig(
            ctx.configurations_dir / folder / f"{prefix}.xcconfig",
            target_common_no_sign,
            project_name,
            includes=["../sign.xcconfig"],
            header_comment=f"Settings specific to {target_name} target",
            dry_run=dry_run
        )

        # Write target debug config
        write_xcconfig(
            ctx.configurations_dir / folder / f"{prefix}-debug.xcconfig",
            target_debug if target_debug else {},
            project_name,
            includes=["../debug.xcconfig", f"{prefix}.xcconfig"],
            header_comment=f"Debug settings for {target_name}",
            dry_run=dry_run
        )

        # Write target release config
        write_xcconfig(
            ctx.configurations_dir / folder / f"{prefix}-release.xcconfig",
            target_release if target_release else {},
            project_name,
            includes=["../release.xcconfig", f"{prefix}.xcconfig"],
            header_comment=f"Release settings for {target_name}",
            dry_run=dry_run
        )

    return target_info


def generate_uuid() -> str:
    """Generate a UUID-like string for pbxproj."""
    return ''.join(random.choices('0123456789ABCDEF', k=24))


def update_pbxproj_with_xcconfig_references(content: str, parsed: dict,
                                            target_info: dict, dry_run: bool = False) -> str:
    """Add xcconfig file references to pbxproj and link them to configurations."""
    print("\n=== Updating pbxproj with xcconfig references ===")

    project_name = parsed["project_name"]
    main_group_uuid = parsed["main_group_uuid"]

    file_refs = {}
    group_refs = {}

    # Build list of xcconfig files dynamically
    xcconfig_files = [
        ("sign.xcconfig", "sign.xcconfig", "Configurations"),
        ("base.xcconfig", "base.xcconfig", "Configurations"),
        ("debug.xcconfig", "debug.xcconfig", "Configurations"),
        ("release.xcconfig", "release.xcconfig", "Configurations"),
    ]

    # Add target-specific xcconfig files
    for target_name, info in target_info.items():
        folder = info["folder"]
        prefix = info["prefix"]
        xcconfig_files.extend([
            (f"{folder}/{prefix}.xcconfig", f"{prefix}.xcconfig", folder),
            (f"{folder}/{prefix}-debug.xcconfig", f"{prefix}-debug.xcconfig", folder),
            (f"{folder}/{prefix}-release.xcconfig", f"{prefix}-release.xcconfig", folder),
        ])

    # Generate UUIDs for files
    for key, _, _ in xcconfig_files:
        file_refs[key] = generate_uuid()

    # Generate UUIDs for groups
    group_refs["Configurations"] = generate_uuid()
    for target_name, info in target_info.items():
        folder = info["folder"]
        if folder not in group_refs:
            group_refs[folder] = generate_uuid()

    # Build PBXFileReference entries
    file_ref_entries = []
    for key, filename, group in xcconfig_files:
        uuid = file_refs[key]
        file_ref_entries.append(
            f'\t\t{uuid} /* {filename} */ = {{isa = PBXFileReference; lastKnownFileType = text.xcconfig; path = {filename}; sourceTree = "<group>"; }};'
        )

    # Build main Configurations group children
    main_group_children = [
        f'{file_refs["sign.xcconfig"]} /* sign.xcconfig */,',
        f'{file_refs["base.xcconfig"]} /* base.xcconfig */,',
        f'{file_refs["debug.xcconfig"]} /* debug.xcconfig */,',
        f'{file_refs["release.xcconfig"]} /* release.xcconfig */,',
    ]

    # Add subgroup references
    added_folders = set()
    for target_name, info in target_info.items():
        folder = info["folder"]
        if folder not in added_folders:
            main_group_children.append(f'{group_refs[folder]} /* {folder} */,')
            added_folders.add(folder)

    # Build main Configurations group
    group_entries = [f'''
\t\t{group_refs["Configurations"]} /* Configurations */ = {{
\t\t\tisa = PBXGroup;
\t\t\tchildren = (
\t\t\t\t{chr(10).join([chr(9)*4 + c for c in main_group_children])}
\t\t\t);
\t\t\tpath = Configurations;
\t\t\tsourceTree = "<group>";
\t\t}};''']

    # Build subgroups
    for folder in added_folders:
        # Find all files in this folder
        children = []
        for key, filename, grp in xcconfig_files:
            if grp == folder:
                children.append(f'{file_refs[key]} /* {filename} */,')

        group_entries.append(f'''
\t\t{group_refs[folder]} /* {folder} */ = {{
\t\t\tisa = PBXGroup;
\t\t\tchildren = (
\t\t\t\t{chr(10).join([chr(9)*4 + c for c in children])}
\t\t\t);
\t\t\tpath = {folder};
\t\t\tsourceTree = "<group>";
\t\t}};''')

    # Add file references to PBXFileReference section
    file_ref_section_end = "/* End PBXFileReference section */"
    new_file_refs = '\n'.join(file_ref_entries)
    content = content.replace(file_ref_section_end, f'{new_file_refs}\n{file_ref_section_end}')

    # Add groups to PBXGroup section
    group_section_end = "/* End PBXGroup section */"
    new_groups = '\n'.join(group_entries)
    content = content.replace(group_section_end, f'{new_groups}\n{group_section_end}')

    # Add Configurations group to main group
    if main_group_uuid:
        main_group_pattern = re.compile(
            rf'({re.escape(main_group_uuid)}\s*=\s*\{{\s*isa\s*=\s*PBXGroup;\s*children\s*=\s*\()',
            re.MULTILINE
        )
        content = main_group_pattern.sub(
            rf'\1\n\t\t\t\t{group_refs["Configurations"]} /* Configurations */,',
            content
        )

    # Build config map for linking xcconfig to build configurations
    config_map = {
        ("PBXProject", "Debug"): file_refs["debug.xcconfig"],
        ("PBXProject", "Release"): file_refs["release.xcconfig"],
    }

    for target_name, info in target_info.items():
        folder = info["folder"]
        prefix = info["prefix"]
        config_map[(target_name, "Debug")] = file_refs[f"{folder}/{prefix}-debug.xcconfig"]
        config_map[(target_name, "Release")] = file_refs[f"{folder}/{prefix}-release.xcconfig"]

    # Add baseConfigurationReference to each XCBuildConfiguration
    for (target_type, config_name), xcconfig_uuid in config_map.items():
        if target_type == "PBXProject":
            pattern = re.compile(
                rf'(/\*\s*{config_name}\s+configuration\s+for\s+PBXProject\s+"[^"]+"\s*\*/\s*=\s*\{{\s*isa\s*=\s*XCBuildConfiguration;)',
                re.MULTILINE
            )
        else:
            pattern = re.compile(
                rf'(/\*\s*{config_name}\s+configuration\s+for\s+PBXNativeTarget\s+"{re.escape(target_type)}"\s*\*/\s*=\s*\{{\s*isa\s*=\s*XCBuildConfiguration;)',
                re.MULTILINE
            )

        xcconfig_path = [k for k, v in file_refs.items() if v == xcconfig_uuid][0]
        xcconfig_name = os.path.basename(xcconfig_path)

        replacement = rf'\1\n\t\t\tbaseConfigurationReference = {xcconfig_uuid} /* {xcconfig_name} */;'
        content = pattern.sub(replacement, content)

    if dry_run:
        print("[DRY RUN] Would update pbxproj with xcconfig references")
    else:
        print("  Added file references for xcconfig files")
        print("  Added group hierarchy for Configurations folder")
        print("  Linked xcconfig files to build configurations")

    return content


def clean_build_settings_from_pbxproj(content: str, dry_run: bool = False) -> str:
    """Remove inline build settings from pbxproj."""
    print("\n=== Cleaning build settings from pbxproj ===")

    pattern = re.compile(
        r'(buildSettings\s*=\s*)\{[^}]+\}',
        re.MULTILINE | re.DOTALL
    )

    def replacer(match):
        return match.group(1) + '{\n\t\t\t}'

    new_content = pattern.sub(replacer, content)

    if dry_run:
        print("[DRY RUN] Would clean build settings from pbxproj")
    else:
        print("  Removed inline build settings from all configurations")

    return new_content


def backup_pbxproj(ctx: ProjectContext, dry_run: bool = False):
    """Create backup of original pbxproj."""
    if dry_run:
        print(f"[DRY RUN] Would backup {ctx.pbxproj_path} to {ctx.backup_path}")
    else:
        shutil.copy2(ctx.pbxproj_path, ctx.backup_path)
        print(f"Backup created: {ctx.backup_path}")


def rollback_pbxproj(ctx: ProjectContext):
    """Restore original pbxproj from backup."""
    if ctx.backup_path.exists():
        shutil.copy2(ctx.backup_path, ctx.pbxproj_path)
        print(f"Restored: {ctx.pbxproj_path} from {ctx.backup_path}")
    else:
        print(f"Error: Backup file not found at {ctx.backup_path}")


def find_xcodeproj(path: str) -> Path:
    """Find xcodeproj file from path (can be directory or file)."""
    p = Path(path)

    if p.suffix == ".xcodeproj":
        if p.exists():
            return p
        raise FileNotFoundError(f"Project not found: {p}")

    # If directory, look for .xcodeproj inside
    if p.is_dir():
        xcodeprojs = list(p.glob("*.xcodeproj"))
        if len(xcodeprojs) == 1:
            return xcodeprojs[0]
        elif len(xcodeprojs) > 1:
            raise ValueError(f"Multiple .xcodeproj found in {p}. Please specify one explicitly.")
        else:
            raise FileNotFoundError(f"No .xcodeproj found in {p}")

    raise FileNotFoundError(f"Path not found: {p}")


def main():
    parser = argparse.ArgumentParser(
        description="Extract build settings from xcodeproj to xcconfig files",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --project MyApp.xcodeproj --dry-run
  %(prog)s --project MyApp.xcodeproj --extract-only
  %(prog)s --project MyApp.xcodeproj
  %(prog)s --project MyApp.xcodeproj --rollback
  %(prog)s --project .  # Auto-detect xcodeproj in current directory
"""
    )
    parser.add_argument("--project", "-p", required=True,
                        help="Path to .xcodeproj file or directory containing it")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview changes without modifying files")
    parser.add_argument("--extract-only", action="store_true",
                        help="Only extract to xcconfig, don't modify pbxproj")
    parser.add_argument("--rollback", action="store_true",
                        help="Restore original pbxproj from backup")
    args = parser.parse_args()

    # Find and validate xcodeproj
    try:
        xcodeproj_path = find_xcodeproj(args.project)
    except (FileNotFoundError, ValueError) as e:
        print(f"Error: {e}")
        return 1

    ctx = ProjectContext(xcodeproj_path)

    if not ctx.pbxproj_path.exists():
        print(f"Error: project.pbxproj not found in {ctx.xcodeproj_path}")
        return 1

    if args.rollback:
        rollback_pbxproj(ctx)
        return 0

    print("=" * 60)
    print(f"Build Settings Extraction Tool")
    print(f"Project: {ctx.project_name}")
    print("=" * 60)

    # Read pbxproj
    print(f"\nReading: {ctx.pbxproj_path}")
    content = ctx.pbxproj_path.read_text()

    # Parse
    print("Parsing build configurations...")
    parsed = parse_pbxproj(content)

    if not parsed["project_debug"] or not parsed["project_release"]:
        print("Error: Could not find project-level Debug/Release configurations")
        return 1

    # Summary
    print(f"\nFound configurations:")
    print(f"  Project: {parsed['project_name']}")
    print(f"  Project Debug: {parsed['project_debug'].uuid}")
    print(f"  Project Release: {parsed['project_release'].uuid}")
    for target, configs in parsed["targets"].items():
        print(f"  Target '{target}':")
        print(f"    Debug: {configs.get('debug', {}).uuid if configs.get('debug') else 'Not found'}")
        print(f"    Release: {configs.get('release', {}).uuid if configs.get('release') else 'Not found'}")

    # Create backup before any modifications
    if not args.dry_run and not args.extract_only:
        backup_pbxproj(ctx, args.dry_run)

    # Generate xcconfig files
    target_info = generate_xcconfig_files(ctx, parsed, args.dry_run)

    if args.extract_only:
        print("\n[extract-only] Skipping pbxproj modifications")
        return 0

    # Update pbxproj with references
    content = update_pbxproj_with_xcconfig_references(content, parsed, target_info, args.dry_run)

    # Clean build settings
    content = clean_build_settings_from_pbxproj(content, args.dry_run)

    # Write updated pbxproj
    if not args.dry_run:
        ctx.pbxproj_path.write_text(content)
        print(f"\nUpdated: {ctx.pbxproj_path}")

    print("\n" + "=" * 60)
    print("Done!")
    print("=" * 60)
    print("\nNext steps:")
    print(f"1. Verify: xcodebuild -showBuildSettings -project {ctx.xcodeproj_path.name} -scheme {ctx.project_name}")
    print(f"2. Build:  xcodebuild -project {ctx.xcodeproj_path.name} -scheme {ctx.project_name} -destination 'platform=macOS'")
    print(f"3. If issues: python {Path(__file__).name} --project {ctx.xcodeproj_path.name} --rollback")

    return 0


if __name__ == "__main__":
    exit(main())
