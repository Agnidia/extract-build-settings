# extract-build-settings

Extract build settings from Xcode project files (`.xcodeproj`) to organized `.xcconfig` files.

## Overview

This tool automates the migration of inline build settings from `project.pbxproj` to external `.xcconfig` files. It:

1. **Parses** the `project.pbxproj` file to extract all build configurations
2. **Generates** organized `.xcconfig` files with settings grouped by category
3. **Updates** the `pbxproj` to reference the new `.xcconfig` files
4. **Removes** inline build settings from the `pbxproj`

## Why Use xcconfig Files?

- **Version Control**: Easier to review and merge build setting changes
- **Reusability**: Share settings across multiple targets and projects
- **Organization**: Settings grouped by category (Signing, Deployment, Swift, etc.)
- **Flexibility**: Override settings per-configuration or per-target
- **Team Collaboration**: Separate signing settings can be gitignored for personal certificates

## Installation

```bash
# Clone the repository
git clone https://github.com/igrsoft/extract-build-settings.git
cd extract-build-settings

# No dependencies required - uses Python 3 standard library only
```

## Usage

```bash
# Preview changes (recommended first step)
python extract_build_settings.py --project MyApp.xcodeproj --dry-run

# Extract settings to xcconfig files only (don't modify pbxproj)
python extract_build_settings.py --project MyApp.xcodeproj --extract-only

# Full extraction and pbxproj update
python extract_build_settings.py --project MyApp.xcodeproj

# Rollback to original pbxproj from backup
python extract_build_settings.py --project MyApp.xcodeproj --rollback

# Auto-detect xcodeproj in current directory
python extract_build_settings.py --project .
```

### Options

| Option | Description |
|--------|-------------|
| `--project`, `-p` | Path to `.xcodeproj` file or directory containing it (required) |
| `--dry-run` | Preview changes without modifying any files |
| `--extract-only` | Generate xcconfig files but don't modify pbxproj |
| `--rollback` | Restore original pbxproj from backup |

## Generated Structure

```
MyApp/
├── MyApp.xcodeproj/
│   └── project.pbxproj          # Updated with xcconfig references
├── Configurations/
│   ├── sign.xcconfig            # Code signing settings (gitignore-able)
│   ├── base.xcconfig            # Shared settings for all configurations
│   ├── debug.xcconfig           # Debug-specific settings
│   ├── release.xcconfig         # Release-specific settings
│   ├── app/
│   │   ├── app.xcconfig         # Main app target settings
│   │   ├── app-debug.xcconfig   # App debug settings
│   │   └── app-release.xcconfig # App release settings
│   ├── tests/
│   │   ├── test.xcconfig        # Unit tests settings
│   │   ├── test-debug.xcconfig
│   │   └── test-release.xcconfig
│   └── uitests/
│       ├── uitest.xcconfig      # UI tests settings
│       ├── uitest-debug.xcconfig
│       └── uitest-release.xcconfig
```

## xcconfig Hierarchy

The generated files follow a logical inheritance pattern:

```
sign.xcconfig          ← Code signing (DEVELOPMENT_TEAM, CODE_SIGN_IDENTITY, etc.)
    ↑
base.xcconfig          ← Common settings (includes sign.xcconfig)
    ↑
debug.xcconfig         ← Debug-specific (includes base.xcconfig)
release.xcconfig       ← Release-specific (includes base.xcconfig)
    ↑
app-debug.xcconfig     ← Target + configuration (includes debug.xcconfig + app.xcconfig)
```

## Setting Categories

Settings are automatically organized into Xcode's standard categories:

- **Architectures**: `ARCHS`, `VALID_ARCHS`, `SDKROOT`, `SUPPORTED_PLATFORMS`
- **Build Options**: `DEBUG_INFORMATION_FORMAT`, `ENABLE_TESTABILITY`, `ENABLE_BITCODE`
- **Deployment**: `IPHONEOS_DEPLOYMENT_TARGET`, `MACOSX_DEPLOYMENT_TARGET`, `SKIP_INSTALL`
- **Signing**: `DEVELOPMENT_TEAM`, `CODE_SIGN_STYLE`, `CODE_SIGN_IDENTITY`
- **Packaging**: `PRODUCT_NAME`, `PRODUCT_BUNDLE_IDENTIFIER`, `INFOPLIST_FILE`
- **Info.plist Values**: `INFOPLIST_KEY_*` settings
- **Linking**: `LD_RUNPATH_SEARCH_PATHS`, `OTHER_LDFLAGS`, `DEAD_CODE_STRIPPING`
- **Search Paths**: `FRAMEWORK_SEARCH_PATHS`, `HEADER_SEARCH_PATHS`
- **Apple Clang**: Code generation, language, warnings, and analyzer settings
- **Swift Compiler**: `SWIFT_*` settings
- **Asset Catalog Compiler**: `ASSETCATALOG_*` settings
- **Metal Compiler**: `MTL_*` settings
- **User-Defined**: Custom settings not matching standard categories

## Workflow

### Recommended Steps

1. **Backup** your project (or ensure it's committed to git)

2. **Preview** the changes:
   ```bash
   python extract_build_settings.py --project MyApp.xcodeproj --dry-run
   ```

3. **Extract** settings:
   ```bash
   python extract_build_settings.py --project MyApp.xcodeproj
   ```

4. **Verify** the build:
   ```bash
   xcodebuild -showBuildSettings -project MyApp.xcodeproj -scheme MyApp
   xcodebuild -project MyApp.xcodeproj -scheme MyApp -destination 'platform=iOS Simulator,name=iPhone 16'
   ```

5. **Rollback** if needed:
   ```bash
   python extract_build_settings.py --project MyApp.xcodeproj --rollback
   ```

### Gitignore Recommendation

Add personal signing settings to `.gitignore`:

```gitignore
# Personal code signing - each developer can have their own
Configurations/sign.xcconfig
```

Then create a template for team members:

```bash
cp Configurations/sign.xcconfig Configurations/sign.xcconfig.template
```

## Requirements

- Python 3.6+
- No external dependencies

## Limitations

- Assumes standard Debug/Release configuration names
- Targets are auto-detected based on naming conventions (Tests, UITests, etc.)
- Complex conditional settings may need manual adjustment

## License

MIT License
