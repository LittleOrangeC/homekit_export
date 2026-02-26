# HomeKit Export

Export, inspect, and backup Apple HomeKit databases on macOS. Extracts scene definitions, accessory-to-room mappings, and automation data from the local SQLite databases managed by the `homed` daemon.

Designed for backup/restore workflows — particularly useful before re-pairing a Hue Bridge or other accessories where HomeKit scenes and room assignments would otherwise be lost.

## Features

- **Database export** — Creates consistent SQLite snapshots using the online backup API (no need to stop `homed`)
- **Scene extraction** — Decodes all HomeKit scenes with human-readable action values (power state, brightness, hue, saturation, color temperature, lock state, rotation speed)
- **Room mapping export** — Captures every accessory-to-room assignment for restore after re-pairing
- **Binary value decoder** — Reverse-engineered HomeKit's compact binary target value format (type-tagged uint8/uint16/float32/float64 encoding)
- **Interactive SQL shell** — Query the exported databases directly with tab completion, CSV export, and cross-database scanning
- **Multiple output formats** — JSON (for programmatic restore), CSV (for spreadsheet review), and human-readable terminal output

## Requirements

- macOS with HomeKit configured (tested on macOS Tahoe 26.x, should work on Sequoia 15.x and Ventura 13.x+)
- Python 3.8+ (system Python works fine — no pip dependencies)
- **Full Disk Access** for your terminal app

### Granting Full Disk Access

The HomeKit database at `~/Library/HomeKit/` is protected by macOS TCC. Your terminal app needs Full Disk Access to read it:

1. Open **System Settings → Privacy & Security → Full Disk Access**
2. Click the **+** button
3. Add your terminal app (Terminal.app, iTerm2, Warp, etc.)
4. Restart your terminal

## Quick Start

```bash
# Full backup — databases + scenes + room mappings
python3 homekit_export.py --backup-all

# Export only scene definitions
python3 homekit_export.py --scenes

# Export only accessory→room mappings
python3 homekit_export.py --rooms

# Peek at the database schema without exporting
python3 homekit_export.py --schema-only

# Interactive SQL query mode
python3 homekit_export.py --interactive
```

All exports are saved to `~/Desktop/homekit_export/<timestamp>/` by default.

## Usage

```
python3 homekit_export.py [OPTIONS]

Options:
  --backup-all          Full backup: databases + scenes + room mappings
  --scenes              Decode and export all HomeKit scenes
  --rooms               Export all accessory→room mappings
  --interactive, -i     Drop into interactive SQL query mode after export
  --dump-all, -d        Dump all table contents to stdout
  --schema-only, -s     Show database schema without exporting
  --output-dir, -o DIR  Output directory (default: ~/Desktop/homekit_export)
```

## Output Files

A `--backup-all` export produces:

| File | Description |
|---|---|
| `scenes_export.json` | All scenes with decoded actions, keyed by accessory name + room |
| `scenes_export.csv` | Flat spreadsheet view of all scene actions |
| `accessory_rooms.json` | Every accessory and its assigned room |
| `accessory_rooms.csv` | Flat spreadsheet view of accessory→room mappings |
| `export_metadata.json` | Timestamp, macOS version, source directory |
| `*.sqlite` | Consistent snapshots of all HomeKit databases |

### scenes_export.json structure

```json
{
  "format_version": 1,
  "homes": {
    "My Home": [
      {
        "name": "Reading",
        "actions": [
          {
            "accessory_name": "Dining Puzzle",
            "room": "Dining Room",
            "manufacturer": "Signify Netherlands B.V.",
            "model": "LCA009",
            "property": "Brightness",
            "value": "85%",
            "raw_hex": "3055"
          }
        ]
      }
    ]
  }
}
```

### accessory_rooms.json structure

```json
{
  "format_version": 1,
  "homes": {
    "My Home": [
      {
        "name": "Kitchen 1",
        "room": "Kitchen",
        "manufacturer": "Signify Netherlands B.V.",
        "model": "LCA009"
      }
    ]
  }
}
```

## Use Cases

### Backup before Hue Bridge re-pair

When you reset the connection between a Hue Bridge and HomeKit, all lights are re-added to rooms but every scene is wiped. This tool lets you back up scene definitions and room assignments before the reset, then restore them programmatically afterward.

```bash
# 1. Before the reset — full backup
python3 homekit_export.py --backup-all

# 2. Reset and re-pair the Hue Bridge in the Home app
#    (lights reappear with same names but new internal IDs)

# 3. Restore room assignments (requires HomeClaw)
homeclaw-cli assign-rooms \
  --file ~/Desktop/homekit_export/<timestamp>/accessory_rooms.json \
  --home "My Home"

# 4. Restore all scenes (requires HomeClaw)
homeclaw-cli import-scene \
  --file ~/Desktop/homekit_export/<timestamp>/scenes_export.json \
  --home "My Home"
```

See [Restoring with HomeClaw](#restoring-with-homeclaw) for setup instructions.

### Inspect and audit your HomeKit setup

Use the interactive mode to explore your HomeKit database with SQL:

```bash
python3 homekit_export.py --interactive
```

```sql
-- List non-empty tables across all databases
.scan

-- Switch to the main database
USE 3

-- List all rooms and their accessory counts
SELECT r.ZNAME as room, COUNT(a.Z_PK) as accessories
FROM ZMKFROOM r
LEFT JOIN ZMKFACCESSORY a ON a.ZROOM = r.Z_PK
GROUP BY r.ZNAME ORDER BY accessories DESC;

-- Find scenes with the most actions
SELECT s.ZNAME, COUNT(a.Z_PK) as actions
FROM ZMKFACTIONSET s
LEFT JOIN ZMKFACTION a ON a.ZACTIONSET = s.Z_PK
GROUP BY s.ZNAME ORDER BY actions DESC;

-- Export query results to CSV
.export SELECT * FROM ZMKFACCESSORY
```

### Document your smart home inventory

Generate a spreadsheet of every accessory, its room, manufacturer, and model:

```bash
python3 homekit_export.py --rooms
# Opens accessory_rooms.csv in your export directory
```

### Pre-migration snapshot

Before a major macOS upgrade, take a full database snapshot:

```bash
python3 homekit_export.py --backup-all
```

The raw `.sqlite` files are included alongside the decoded exports as an additional safety net.

## How It Works

### Database Location

HomeKit stores its data in `~/Library/HomeKit/` as SQLite databases managed by the `homed` daemon. On macOS Tahoe (26.x), the key databases are:

| Database | Contents |
|---|---|
| `core.sqlite` | Accessories, rooms, scenes, automations, users, zones |
| `core-cloudkit.sqlite` | CloudKit sync mirror of core data |
| `core-cloudkit-shared.sqlite` | Shared home data (for multi-user homes) |
| `core-local.sqlite` | Local-only settings and notification registrations |
| `datastore.sqlite` | Legacy CloudKit data store |
| `datastore3.sqlite` | Camera clips, face recognition, activity data |
| `eventstore-beta.sqlite` | Event tracking |

### Export Strategy

The script uses SQLite's online backup API (`connection.backup()`) to create consistent snapshots while `homed` is running. After backup, it runs `PRAGMA wal_checkpoint(TRUNCATE)` on the copies to consolidate any WAL journal data into the main database file, producing self-contained exports.

### Binary Value Decoding

HomeKit stores scene action target values in a compact binary format. The encoding scheme (reverse-engineered from macOS Tahoe):

| First Byte | Format | Decode Method |
|---|---|---|
| `0x01`–`0x2F` | Direct value | The byte itself is the value |
| `0x30` | uint8 | Next 1 byte |
| `0x31` | uint16 LE | Next 2 bytes |
| `0x35` | float32 LE | Next 4 bytes |
| `0x36` | float64 LE | Next 8 bytes |

Power state values: `0x01` = ON, `0x02` = OFF (older encoding), `0x08` = ON, `0x09` = OFF (newer encoding).

### Matching Strategy

All exports are keyed by **accessory name + room** rather than internal UUIDs. This is critical for restore workflows — when you re-pair a Hue Bridge or replace an accessory, the internal HomeKit UUIDs change, but accessory names persist (Hue stores names on the bridge itself).

## Restoring with HomeClaw

Scene and room restoration requires [HomeClaw](https://github.com/omarshahine/HomeClaw), an open-source macOS app that provides programmatic access to HomeKit.

### Prerequisites

- Apple Developer account ($99/year) — required for the HomeKit entitlement
- Xcode 26+
- HomeClaw built and installed per its [setup instructions](https://github.com/omarshahine/HomeClaw#quick-start)

### Scene import and room assignment commands

HomeClaw needs two additional commands (`import-scene` and `assign-rooms`) that are not yet in the main repository. These can be added by applying the patches in the [HomeClaw Patches](#homeclaw-patches) section below, or by following the implementation guide.

```bash
# Preview room assignments (dry run)
homeclaw-cli assign-rooms \
  --file accessory_rooms.json \
  --home "My Home" \
  --dry-run

# Apply room assignments
homeclaw-cli assign-rooms \
  --file accessory_rooms.json \
  --home "My Home"

# Preview scene import (dry run)
homeclaw-cli import-scene \
  --file scenes_export.json \
  --scene "Reading" \
  --home "My Home" \
  --dry-run

# Import a single scene
homeclaw-cli import-scene \
  --file scenes_export.json \
  --scene "Reading" \
  --home "My Home"

# Import all scenes for a home
homeclaw-cli import-scene \
  --file scenes_export.json \
  --home "My Home"

# Delete a scene (required before re-importing an existing scene)
homeclaw-cli delete-scene "Reading" --home "My Home"
```

## Interactive Mode Commands

| Command | Description |
|---|---|
| `.tables` | List all tables in the current database |
| `.nonempty` | List only tables with data, sorted by row count |
| `.scan` | Scan all databases for non-empty tables |
| `.schema <table>` | Show the CREATE statement for a table |
| `.dump <table>` | Show table contents (limit 50 rows) |
| `.export <SQL>` | Run a query and save results as CSV |
| `USE <n>` | Switch to database by index |
| Any SQL | Execute a read-only SQL query |
| `.quit` | Exit interactive mode |

## Database Schema (macOS Tahoe)

Key tables in `core.sqlite` for scene/accessory data:

| Table | Rows | Description |
|---|---|---|
| `ZMKFHOME` | Homes | Home names and settings |
| `ZMKFROOM` | Rooms | Room names and home associations |
| `ZMKFACCESSORY` | Accessories | Devices with names, manufacturers, room assignments |
| `ZMKFSERVICE` | Services | HAP services (light, fan, lock, etc.) per accessory |
| `ZMKFCHARACTERISTIC` | Characteristics | Individual controllable properties (brightness, power, etc.) |
| `ZMKFACTIONSET` | Scenes | Scene names and home associations |
| `ZMKFACTION` | Actions | Individual actions within scenes (target values) |
| `ZMKFTRIGGER` | Automations | Automation triggers and conditions |
| `ZMKFZONE` | Zones | Room groupings |

### Key relationships

- Actions → Scenes: `ZMKFACTION.ZACTIONSET` → `ZMKFACTIONSET.Z_PK`
- Actions → Accessories: `ZMKFACTION.ZACCESSORY1` → `ZMKFACCESSORY.Z_PK`
- Actions → Characteristics: `ZMKFACTION.ZCHARACTERISTICID` → `ZMKFCHARACTERISTIC.ZINSTANCEID` (scoped by service)
- Accessories → Rooms: `ZMKFACCESSORY.ZROOM` → `ZMKFROOM.Z_PK`
- Accessories use `ZCONFIGUREDNAME` (user-set) or `ZPROVIDEDNAME` (manufacturer default)

## Limitations

- **Read-only** — This tool exports data but cannot write back to the HomeKit database. Restoration requires HomeClaw or manual re-creation.
- **macOS only** — HomeKit databases are only accessible on macOS, not iOS (without jailbreaking).
- **Full Disk Access required** — The `~/Library/HomeKit/` directory is TCC-protected.
- **Schema may vary** — The CoreData schema can change between macOS versions. Tested on macOS Tahoe 26.3. Column names may differ on older versions.
- **iCloud sync** — HomeKit data syncs via CloudKit. The export captures a point-in-time snapshot of the local database.

## Troubleshooting

**"Permission denied reading ~/Library/HomeKit"**
Grant Full Disk Access to your terminal app in System Settings → Privacy & Security → Full Disk Access.

**"No .sqlite files found"**
HomeKit may not be configured on this Mac. Open the Home app and verify you have at least one home set up.

**Exported databases show empty tables**
The WAL checkpoint may have failed. Try re-running the export — the script uses `PRAGMA wal_checkpoint(TRUNCATE)` to consolidate WAL data.

**Schema errors (no such column)**
The database schema varies between macOS versions. Use `--interactive` mode with `.schema <table>` to inspect the actual column names on your system.

## License

MIT

## Acknowledgments

- [HomeClaw](https://github.com/omarshahine/HomeClaw) by Omar Shahine — HomeKit bridge for programmatic access. Scene import, room assignment, and delete commands added via [PR #7](https://github.com/omarshahine/HomeClaw/pull/7).
- Built with Claude (Anthropic) through iterative reverse-engineering of the HomeKit database format

Written by Matt Tomlinson & Claude.
