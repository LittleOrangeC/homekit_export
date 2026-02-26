#!/usr/bin/env python3
"""
HomeKit Database Export & Inspector
====================================
Exports and inspects the local HomeKit SQLite databases on macOS.

Prerequisites:
  - macOS with HomeKit configured
  - Terminal/iTerm (or your Python interpreter) must have Full Disk Access:
    System Settings → Privacy & Security → Full Disk Access → enable your terminal app
  - Python 3.8+ (system Python works fine)

Usage:
  python3 homekit_export.py                  # Export + summary
  python3 homekit_export.py --interactive    # Export + drop into query mode
  python3 homekit_export.py --dump-all       # Export + dump all table contents
  python3 homekit_export.py --scenes         # Export + decode all scenes
  python3 homekit_export.py --output-dir /path/to/dir  # Custom output directory
"""

import argparse
import csv
import os
import shutil
import sqlite3
import struct
import sys
import json
from datetime import datetime
from pathlib import Path
from textwrap import indent


# --- Configuration -----------------------------------------------------------

HOMEKIT_DIR = Path.home() / "Library" / "HomeKit"
DB_FILES = ["datastore.sqlite", "datastore3.sqlite"]
DEFAULT_OUTPUT = Path.home() / "Desktop" / "homekit_export"


# --- Helpers -----------------------------------------------------------------

def color(text, code):
    if sys.stdout.isatty():
        return f"\033[{code}m{text}\033[0m"
    return text

def info(msg):  print(color(f"[INFO] {msg}", "36"))
def warn(msg):  print(color(f"[WARN] {msg}", "33"))
def err(msg):   print(color(f"[ERR]  {msg}", "31"))
def ok(msg):    print(color(f"[OK]   {msg}", "32"))


# --- Target Value Decoder ----------------------------------------------------

def decode_target_value(raw_bytes, fmt=""):
    """
    Decode HomeKit's compact binary target values.

    Encoding scheme (empirically determined from macOS Tahoe):
      Single byte (no tag): the byte IS the value
      0x30 + 1 byte:  uint8
      0x31 + 2 bytes: uint16 LE
      0x35 + 4 bytes: float32 LE
      0x36 + 8 bytes: float64 LE

    For Power State (bool):
      0x01 = ON,  0x02 = OFF   (older encoding)
      0x08 = ON,  0x09 = OFF   (newer encoding)

    For Lock Target State (uint8):
      0x08 = Secured (locked)
      0x09 = Unsecured (unlocked)
    """
    if raw_bytes is None or len(raw_bytes) == 0:
        return None

    tag = raw_bytes[0]
    data = raw_bytes[1:]

    # Tagged formats
    if tag == 0x30 and len(data) == 1:
        return int(data[0])
    if tag == 0x31 and len(data) >= 2:
        return int.from_bytes(data[:2], "little")
    if tag == 0x35 and len(data) >= 4:
        return round(struct.unpack("<f", data[:4])[0], 2)
    if tag == 0x36 and len(data) >= 8:
        return round(struct.unpack("<d", data[:8])[0], 2)

    # Untagged single-byte value
    if len(raw_bytes) == 1:
        return int(raw_bytes[0])

    # Fallback: return hex representation
    return f"0x{raw_bytes.hex()}"


def format_power_value(val):
    """Interpret decoded power state value as human-readable."""
    if val in (1, 8):
        return "ON"
    if val in (0, 2, 9):
        return "OFF"
    return str(val)


def format_lock_value(val):
    """Interpret decoded lock target state."""
    if val in (1, 8):
        return "Secured (Locked)"
    if val in (0, 9):
        return "Unsecured (Unlocked)"
    return str(val)


def format_action_value(property_name, raw_bytes, fmt=""):
    """Return a human-readable string for an action's target value."""
    val = decode_target_value(raw_bytes, fmt)
    if val is None:
        return None

    prop = (property_name or "").lower()

    if "power" in prop:
        return format_power_value(val)
    if "lock" in prop:
        return format_lock_value(val)
    if "brightness" in prop:
        return f"{val}%"
    if "hue" in prop:
        return f"{val}°"
    if "saturation" in prop:
        return f"{val}%"
    if "color temperature" in prop or "color temp" in prop:
        if isinstance(val, (int, float)) and val > 0:
            kelvin = round(1_000_000 / val) if val > 1 else val
            return f"{val} mireds (~{kelvin}K)"
        return str(val)
    if "rotation" in prop or "speed" in prop:
        return f"{val}%"

    return str(val)


# --- DB Connection -----------------------------------------------------------

def open_readonly(db_path: Path) -> sqlite3.Connection:
    try:
        conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
        conn.execute("SELECT 1")
        return conn
    except sqlite3.OperationalError:
        pass
    try:
        conn = sqlite3.connect(str(db_path.resolve()))
        conn.execute("PRAGMA query_only = ON")
        conn.execute("SELECT 1")
        return conn
    except sqlite3.OperationalError as e:
        raise sqlite3.OperationalError(f"Cannot open {db_path.name}: {e}")


# --- Preflight ---------------------------------------------------------------

def check_platform():
    if sys.platform != "darwin":
        err("This script only runs on macOS.")
        sys.exit(1)

def check_fda():
    if not HOMEKIT_DIR.exists():
        err(f"HomeKit directory not found: {HOMEKIT_DIR}")
        err("Is HomeKit configured on this Mac?")
        sys.exit(1)
    try:
        list(HOMEKIT_DIR.iterdir())
    except PermissionError:
        err(f"Permission denied reading {HOMEKIT_DIR}")
        err("Grant Full Disk Access to your terminal app:")
        err("  System Settings → Privacy & Security → Full Disk Access")
        sys.exit(1)
    ok("Full Disk Access verified — can read HomeKit directory.")

def discover_databases():
    found = []
    for f in HOMEKIT_DIR.iterdir():
        if f.suffix == ".sqlite":
            found.append(f)
    if not found:
        for name in DB_FILES:
            p = HOMEKIT_DIR / name
            if p.exists():
                found.append(p)
    found = sorted(set(found))
    if not found:
        warn("No .sqlite files found in HomeKit directory.")
    return found


# --- Export ------------------------------------------------------------------

def safe_backup(src: Path, dst: Path):
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        src_conn = sqlite3.connect(f"file:{src.resolve()}?mode=ro", uri=True)
        dst_conn = sqlite3.connect(str(dst.resolve()))
        src_conn.backup(dst_conn)
        dst_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        dst_conn.close()
        src_conn.close()
        size = dst.stat().st_size
        ok(f"Backed up {src.name} → {dst.name} ({size:,} bytes)")
        return True
    except sqlite3.OperationalError as e:
        warn(f"SQLite backup failed for {src.name}: {e}")
        warn("Falling back to raw file copy.")
        shutil.copy2(src, dst)
        for suffix in ["-wal", "-shm"]:
            journal = src.parent / (src.name + suffix)
            if journal.exists():
                shutil.copy2(journal, dst.parent / (dst.name + suffix))
                info(f"  Copied journal: {journal.name}")
        ok(f"File-copied {src.name} → {dst.name}")
        return True
    except Exception as e:
        err(f"Failed to export {src.name}: {e}")
        return False


def export_databases(db_files: list, output_dir: Path) -> list:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    export_dir = output_dir / ts
    export_dir.mkdir(parents=True, exist_ok=True)
    info(f"Export directory: {export_dir}")

    exported = []
    for src in db_files:
        dst = export_dir / src.name
        if safe_backup(src, dst):
            exported.append(dst)

    meta = {
        "exported_at": datetime.now().isoformat(),
        "macos_version": os.popen("sw_vers -productVersion").read().strip(),
        "source_dir": str(HOMEKIT_DIR),
        "files": [f.name for f in exported],
    }
    meta_path = export_dir / "export_metadata.json"
    meta_path.write_text(json.dumps(meta, indent=2))
    info(f"Metadata written to {meta_path.name}")
    return exported


# --- Schema Inspection -------------------------------------------------------

def get_schema(db_path: Path) -> dict:
    conn = open_readonly(db_path)
    cursor = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    schema = {row[0]: row[1] for row in cursor.fetchall()}
    conn.close()
    return schema

def get_row_counts(db_path: Path, tables: list) -> dict:
    conn = open_readonly(db_path)
    counts = {}
    for t in tables:
        try:
            count = conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            counts[t] = count
        except sqlite3.OperationalError:
            counts[t] = -1
    conn.close()
    return counts

def print_schema_summary(db_path: Path):
    schema = get_schema(db_path)
    if not schema:
        warn(f"  No tables found in {db_path.name}")
        return
    counts = get_row_counts(db_path, list(schema.keys()))
    print(f"\n{'='*70}")
    print(color(f"  {db_path.name}", "1;37"))
    print(f"  Tables: {len(schema)}  |  Size: {db_path.stat().st_size:,} bytes")
    print(f"{'='*70}")
    for table, ddl in schema.items():
        row_count = counts.get(table, "?")
        print(f"\n  {color(table, '1;36')}  ({row_count} rows)")
        if ddl:
            print(indent(ddl, "    "))
        print()


# --- Scene Export (--scenes) -------------------------------------------------

def extract_scenes(db_path: Path) -> dict:
    """
    Extract all scenes from core.sqlite with decoded actions.

    Returns a dict keyed by home name, each containing a list of scenes.
    Scenes are keyed by accessory NAME + room NAME (not internal IDs),
    so they survive re-pairing when accessories get new UUIDs.
    """
    conn = open_readonly(db_path)

    # 1. Get homes
    homes = {}
    for row in conn.execute("SELECT Z_PK, ZNAME FROM ZMKFHOME"):
        homes[row[0]] = row[1] or f"Home_{row[0]}"

    # 2. Get rooms
    rooms = {}
    for row in conn.execute("SELECT Z_PK, ZNAME, ZHOME FROM ZMKFROOM"):
        rooms[row[0]] = {"name": row[1] or f"Room_{row[0]}", "home_pk": row[2]}

    # 3. Get accessories (name + room for matching after re-pair)
    accessories = {}
    for row in conn.execute(
        "SELECT Z_PK, ZCONFIGUREDNAME, ZPROVIDEDNAME, ZMANUFACTURER, "
        "ZMODEL, ZROOM, ZHOME, ZUNIQUEIDENTIFIER FROM ZMKFACCESSORY"
    ):
        pk, cname, pname, mfr, model, room_pk, home_pk, uid = row
        name = cname or pname or f"Accessory_{pk}"
        room_name = rooms.get(room_pk, {}).get("name", "Unknown Room")
        home_name = homes.get(home_pk, "Unknown Home")
        accessories[pk] = {
            "name": name,
            "configured_name": cname,
            "provided_name": pname,
            "manufacturer": mfr,
            "model": model,
            "room": room_name,
            "home": home_name,
            "uuid": uid,
        }

    # 4. Get services
    services = {}
    for row in conn.execute(
        "SELECT Z_PK, ZNAME, ZPROVIDEDNAME, ZACCESSORY FROM ZMKFSERVICE"
    ):
        services[row[0]] = {
            "name": row[1] or row[2] or f"Service_{row[0]}",
            "accessory_pk": row[3],
        }

    # 5. Build characteristic lookup: (service_pk, instance_id) → description
    characteristics = {}
    for row in conn.execute(
        "SELECT ZSERVICE, ZINSTANCEID, ZMANUFACTURERDESCRIPTION, ZFORMAT, "
        "ZMINIMUMVALUE, ZMAXIMUMVALUE FROM ZMKFCHARACTERISTIC"
    ):
        svc_pk, inst_id, desc, fmt, mn, mx = row
        characteristics[(svc_pk, inst_id)] = {
            "description": desc or "Unknown",
            "format": fmt or "",
            "min": mn,
            "max": mx,
        }

    # 6. Get action sets (scenes)
    action_sets = {}
    for row in conn.execute(
        "SELECT Z_PK, ZNAME, ZHOME, ZTYPE FROM ZMKFACTIONSET"
    ):
        pk, name, home_pk, stype = row
        home_name = homes.get(home_pk, "Unknown Home")
        action_sets[pk] = {
            "name": name or f"Scene_{pk}",
            "home": home_name,
            "home_pk": home_pk,
            "type": stype,
        }

    # 7. Get actions and decode target values
    actions_query = conn.execute(
        "SELECT ZACTIONSET, ZACCESSORY1, ZACCESSORY2, ZSERVICE, "
        "ZCHARACTERISTICID, ZTARGETVALUE, ZVOLUME FROM ZMKFACTION"
    )

    scene_data = {}
    for row in actions_query:
        aset_pk, acc1_pk, acc2_pk, svc_pk, char_id, target_raw, volume = row

        if aset_pk not in action_sets:
            continue

        scene_key = aset_pk
        if scene_key not in scene_data:
            scene_info = action_sets[aset_pk]
            scene_data[scene_key] = {
                "scene_name": scene_info["name"],
                "home": scene_info["home"],
                "scene_type": scene_info.get("type"),
                "actions": [],
                "associated_accessories": [],
            }

        # Determine if this is a real action (acc1 + service) or association (acc2 only)
        if acc1_pk and svc_pk:
            acc_info = accessories.get(acc1_pk, {})
            char_info = characteristics.get((svc_pk, char_id), {})
            prop_name = char_info.get("description", "Unknown")
            prop_fmt = char_info.get("format", "")

            decoded = format_action_value(prop_name, target_raw, prop_fmt)

            scene_data[scene_key]["actions"].append({
                "accessory_name": acc_info.get("name", f"ID_{acc1_pk}"),
                "room": acc_info.get("room", "Unknown"),
                "manufacturer": acc_info.get("manufacturer"),
                "model": acc_info.get("model"),
                "property": prop_name,
                "format": prop_fmt,
                "value": decoded,
                "raw_hex": target_raw.hex() if target_raw else None,
                "volume": volume,
            })
        elif acc2_pk:
            acc_info = accessories.get(acc2_pk, {})
            scene_data[scene_key]["associated_accessories"].append({
                "accessory_name": acc_info.get("name", f"ID_{acc2_pk}"),
                "room": acc_info.get("room", "Unknown"),
                "manufacturer": acc_info.get("manufacturer"),
                "model": acc_info.get("model"),
            })

    conn.close()

    # Organize by home
    by_home = {}
    for sc in scene_data.values():
        home = sc["home"]
        if home not in by_home:
            by_home[home] = []
        by_home[home].append(sc)

    # Sort scenes by name within each home
    for home in by_home:
        by_home[home].sort(key=lambda s: s["scene_name"])

    return by_home


def print_scenes(scenes_by_home: dict):
    """Pretty-print all scenes."""
    total_scenes = 0
    total_actions = 0

    for home_name, scenes in sorted(scenes_by_home.items()):
        print(f"\n{'='*70}")
        print(color(f"  HOME: {home_name}", "1;37"))
        print(f"  {len(scenes)} scenes")
        print(f"{'='*70}")

        for sc in scenes:
            actions = sc["actions"]
            assoc = sc["associated_accessories"]
            total_scenes += 1
            total_actions += len(actions)

            print(f"\n  {color(sc['scene_name'], '1;36')}")
            if not actions and not assoc:
                print(f"    (no actions)")
                continue

            # Group actions by accessory for cleaner display
            by_accessory = {}
            for act in actions:
                key = f"{act['accessory_name']} ({act['room']})"
                if key not in by_accessory:
                    by_accessory[key] = []
                by_accessory[key].append(act)

            for acc_label, acc_actions in sorted(by_accessory.items()):
                props = []
                for a in acc_actions:
                    val_str = a["value"] if a["value"] is not None else "?"
                    props.append(f"{a['property']}: {val_str}")
                props_str = ", ".join(props)
                print(f"    {acc_label:45s} → {props_str}")

            if assoc:
                assoc_names = [f"{a['accessory_name']}" for a in assoc]
                print(color(
                    f"    [also associated: {', '.join(assoc_names)}]", "2"
                ))

    print(f"\n{'─'*70}")
    print(f"  Total: {total_scenes} scenes, {total_actions} actions")
    print(f"{'─'*70}")


def export_scenes_json(scenes_by_home: dict, output_dir: Path):
    """
    Export scenes to JSON structured for re-creation after re-pairing.

    Keyed by accessory name + room (survives re-pairing) rather than
    internal UUIDs/PKs (which change).
    """
    export = {
        "exported_at": datetime.now().isoformat(),
        "format_version": 1,
        "note": (
            "Scenes keyed by accessory name + room for re-creation after "
            "re-pairing. Internal UUIDs will change; match by name + room."
        ),
        "homes": {},
    }

    for home_name, scenes in sorted(scenes_by_home.items()):
        home_scenes = []
        for sc in scenes:
            scene_export = {
                "name": sc["scene_name"],
                "actions": [],
            }
            for act in sc["actions"]:
                scene_export["actions"].append({
                    "accessory_name": act["accessory_name"],
                    "room": act["room"],
                    "manufacturer": act["manufacturer"],
                    "model": act["model"],
                    "property": act["property"],
                    "value": act["value"],
                    "raw_hex": act["raw_hex"],
                })
            home_scenes.append(scene_export)
        export["homes"][home_name] = home_scenes

    json_path = output_dir / "scenes_export.json"
    json_path.write_text(json.dumps(export, indent=2, default=str))
    ok(f"Scene definitions exported to {json_path}")
    return json_path


# --- Accessory Room Mapping Export -------------------------------------------

def extract_accessory_rooms(db_path: Path) -> dict:
    """
    Extract all accessory→room mappings from core.sqlite.
    Keyed by accessory name + home (survives re-pairing).
    """
    conn = open_readonly(db_path)

    # Homes
    homes = {}
    for row in conn.execute("SELECT Z_PK, ZNAME FROM ZMKFHOME"):
        homes[row[0]] = row[1] or f"Home_{row[0]}"

    # Rooms
    rooms = {}
    for row in conn.execute("SELECT Z_PK, ZNAME, ZHOME FROM ZMKFROOM"):
        rooms[row[0]] = {"name": row[1] or f"Room_{row[0]}", "home_pk": row[2]}

    # Accessories
    accessories = []
    for row in conn.execute(
        "SELECT ZCONFIGUREDNAME, ZPROVIDEDNAME, ZMANUFACTURER, ZMODEL, "
        "ZROOM, ZHOME, ZUNIQUEIDENTIFIER FROM ZMKFACCESSORY"
    ):
        cname, pname, mfr, model, room_pk, home_pk, uid = row
        name = cname or pname or "Unknown"
        room_info = rooms.get(room_pk, {})
        room_name = room_info.get("name", "Default Room")
        home_name = homes.get(home_pk, "Unknown Home")

        accessories.append({
            "name": name,
            "configured_name": cname,
            "provided_name": pname,
            "room": room_name,
            "home": home_name,
            "manufacturer": mfr,
            "model": model,
            "uuid": uid,
        })

    conn.close()

    # Organize by home
    by_home = {}
    for acc in accessories:
        home = acc["home"]
        if home not in by_home:
            by_home[home] = []
        by_home[home].append(acc)

    # Sort by room then name within each home
    for home in by_home:
        by_home[home].sort(key=lambda a: (a["room"], a["name"]))

    return by_home


def print_accessory_rooms(rooms_by_home: dict):
    """Pretty-print all accessory→room mappings."""
    total = 0
    for home_name, accessories in sorted(rooms_by_home.items()):
        print(f"\n{'='*70}")
        print(color(f"  HOME: {home_name}", "1;37"))
        print(f"  {len(accessories)} accessories")
        print(f"{'='*70}")

        current_room = None
        for acc in accessories:
            if acc["room"] != current_room:
                current_room = acc["room"]
                print(f"\n  {color(current_room, '1;36')}")
            mfr = acc.get("manufacturer") or ""
            model = acc.get("model") or ""
            suffix = f"  ({mfr} {model})".strip() if mfr or model else ""
            print(f"    {acc['name']}{suffix}")
            total += 1

    print(f"\n{'─'*70}")
    print(f"  Total: {total} accessories")
    print(f"{'─'*70}")


def export_accessory_rooms_json(rooms_by_home: dict, output_dir: Path) -> Path:
    """Export accessory→room mappings to JSON for restore via HomeClaw."""
    export = {
        "exported_at": datetime.now().isoformat(),
        "format_version": 1,
        "note": (
            "Accessory-to-room mappings keyed by name + home. "
            "Use with 'homeclaw-cli assign-rooms' after re-pairing."
        ),
        "homes": {},
    }

    for home_name, accessories in sorted(rooms_by_home.items()):
        home_accs = []
        for acc in accessories:
            home_accs.append({
                "name": acc["name"],
                "room": acc["room"],
                "manufacturer": acc.get("manufacturer"),
                "model": acc.get("model"),
            })
        export["homes"][home_name] = home_accs

    json_path = output_dir / "accessory_rooms.json"
    json_path.write_text(json.dumps(export, indent=2, default=str))
    ok(f"Accessory room mappings exported to {json_path}")
    return json_path


def export_accessory_rooms_csv(rooms_by_home: dict, output_dir: Path) -> Path:
    """Export a flat CSV of accessory→room mappings."""
    csv_path = output_dir / "accessory_rooms.csv"
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Home", "Room", "Accessory", "Manufacturer", "Model"])
        for home_name, accessories in sorted(rooms_by_home.items()):
            for acc in accessories:
                writer.writerow([
                    home_name,
                    acc["room"],
                    acc["name"],
                    acc.get("manufacturer", ""),
                    acc.get("model", ""),
                ])
    ok(f"Accessory rooms CSV exported to {csv_path}")
    return csv_path


def export_scenes_csv(scenes_by_home: dict, output_dir: Path):
    """Export a flat CSV of all scene actions for spreadsheet review."""
    csv_path = output_dir / "scenes_export.csv"
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "Home", "Scene", "Accessory", "Room", "Manufacturer",
            "Model", "Property", "Value", "Raw Hex"
        ])
        for home_name, scenes in sorted(scenes_by_home.items()):
            for sc in scenes:
                for act in sc["actions"]:
                    writer.writerow([
                        home_name,
                        sc["scene_name"],
                        act["accessory_name"],
                        act["room"],
                        act.get("manufacturer", ""),
                        act.get("model", ""),
                        act["property"],
                        act["value"],
                        act.get("raw_hex", ""),
                    ])
    ok(f"Scene actions CSV exported to {csv_path}")
    return csv_path


# --- Table Inspection --------------------------------------------------------

def dump_table(db_path: Path, table: str, limit: int = 50):
    conn = open_readonly(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(f'SELECT * FROM "{table}" LIMIT {limit}').fetchall()
        if not rows:
            print(f"  (empty table)")
            return
        cols = rows[0].keys()
        print(f"\n  {' | '.join(cols)}")
        print(f"  {'-+-'.join('-' * min(len(c), 30) for c in cols)}")
        for row in rows:
            vals = []
            for v in row:
                s = str(v) if v is not None else "NULL"
                if len(s) > 60:
                    s = s[:57] + "..."
                vals.append(s)
            print(f"  {' | '.join(vals)}")
        total = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        if total > limit:
            warn(f"  Showing {limit}/{total} rows. Use --interactive for full access.")
    except sqlite3.OperationalError as e:
        err(f"  Error reading {table}: {e}")
    finally:
        conn.close()

def dump_all_tables(db_path: Path):
    schema = get_schema(db_path)
    for table in schema:
        print(f"\n{'─'*60}")
        print(color(f"  TABLE: {table}", "1;33"))
        print(f"{'─'*60}")
        dump_table(db_path, table)


# --- Interactive Mode --------------------------------------------------------

def interactive_mode(exported: list):
    print(f"\n{'='*70}")
    print(color("  HomeKit DB Interactive Inspector", "1;37"))
    print(f"{'='*70}")
    print("  Available databases:")
    for i, db in enumerate(exported):
        print(f"    [{i}] {db.name}")

    active_db = exported[0]
    if len(exported) > 1:
        print(f"\n  Using: {exported[0].name} (switch with: USE <index>)")

    print(f"\n  Commands:")
    print(f"    .tables           — list all tables")
    print(f"    .nonempty         — list only non-empty tables (with row counts)")
    print(f"    .scan             — scan ALL databases for non-empty tables")
    print(f"    .schema <table>   — show CREATE statement")
    print(f"    .dump <table>     — show table contents (limit 50)")
    print(f"    .export <query>   — run query and save results as CSV")
    print(f"    USE <n>           — switch to database [n]")
    print(f"    Any valid SQL     — run the query")
    print(f"    .quit / exit      — exit")
    print()

    while True:
        try:
            query = input(color(f"  [{active_db.name}]> ", "32")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not query:
            continue
        if query.lower() in (".quit", "exit", "q"):
            break

        if query.lower() == ".tables":
            schema = get_schema(active_db)
            counts = get_row_counts(active_db, list(schema.keys()))
            for t, c in counts.items():
                print(f"    {t} ({c} rows)")
            continue

        if query.lower() == ".nonempty":
            schema = get_schema(active_db)
            counts = get_row_counts(active_db, list(schema.keys()))
            nonempty = {t: c for t, c in counts.items() if c > 0}
            if nonempty:
                for t, c in sorted(nonempty.items(), key=lambda x: -x[1]):
                    print(f"    {t:50s} {c:>8,} rows")
                print(f"\n    {len(nonempty)} non-empty / {len(counts)} total tables")
            else:
                print("    (no non-empty tables)")
            continue

        if query.lower() == ".scan":
            print()
            for i, db in enumerate(exported):
                try:
                    schema = get_schema(db)
                    counts = get_row_counts(db, list(schema.keys()))
                    nonempty = {t: c for t, c in counts.items() if c > 0}
                    total_rows = sum(nonempty.values())
                    print(color(f"  [{i}] {db.name}", "1;36")
                          + f"  ({db.stat().st_size:,} bytes)")
                    if nonempty:
                        for t, c in sorted(nonempty.items(), key=lambda x: -x[1]):
                            print(f"      {t:50s} {c:>8,} rows")
                        print(color(
                            f"      ── {len(nonempty)} non-empty tables, "
                            f"{total_rows:,} total rows ──\n", "2"))
                    else:
                        print(f"      (all tables empty)\n")
                except (sqlite3.OperationalError, sqlite3.DatabaseError) as e:
                    warn(f"  [{i}] {db.name}: {e}\n")
            continue

        if query.lower().startswith(".schema"):
            parts = query.split(maxsplit=1)
            schema = get_schema(active_db)
            if len(parts) > 1 and parts[1] in schema:
                print(f"    {schema[parts[1]]}")
            else:
                for t, ddl in schema.items():
                    print(f"    {ddl}\n")
            continue

        if query.lower().startswith(".dump"):
            parts = query.split(maxsplit=1)
            if len(parts) > 1:
                dump_table(active_db, parts[1])
            else:
                print("    Usage: .dump <table_name>")
            continue

        if query.lower().startswith(".export"):
            parts = query.split(maxsplit=1)
            if len(parts) > 1:
                export_query_to_csv(active_db, parts[1])
            else:
                print("    Usage: .export SELECT * FROM table_name")
            continue

        if query.upper().startswith("USE "):
            try:
                idx = int(query.split()[1])
                active_db = exported[idx]
                ok(f"Switched to {active_db.name}")
            except (IndexError, ValueError):
                err("Usage: USE <index>")
            continue

        conn = open_readonly(active_db)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(query).fetchall()
            if rows:
                cols = rows[0].keys()
                print(f"    {' | '.join(cols)}")
                print(f"    {'-+-'.join('-' * min(len(c), 25) for c in cols)}")
                for row in rows:
                    vals = [str(v) if v is not None else "NULL" for v in row]
                    print(f"    {' | '.join(vals)}")
                print(f"\n    ({len(rows)} rows)")
            else:
                print("    (no results)")
        except sqlite3.Error as e:
            err(f"SQL error: {e}")
        finally:
            conn.close()


def export_query_to_csv(db_path: Path, query: str):
    conn = open_readonly(db_path)
    try:
        cursor = conn.execute(query)
        cols = [desc[0] for desc in cursor.description]
        rows = cursor.fetchall()
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        csv_path = db_path.parent / f"query_export_{ts}.csv"
        with open(csv_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(cols)
            writer.writerows(rows)
        ok(f"Exported {len(rows)} rows to {csv_path}")
    except sqlite3.Error as e:
        err(f"SQL error: {e}")
    finally:
        conn.close()


# --- Main --------------------------------------------------------------------

def find_core_db(sources: list) -> Path:
    """Find core.sqlite from source files (live or exported)."""
    for p in sources:
        if p.name == "core.sqlite":
            return p
    return None


def main():
    parser = argparse.ArgumentParser(
        description="Export and inspect macOS HomeKit databases"
    )
    parser.add_argument(
        "--interactive", "-i", action="store_true",
        help="Drop into interactive SQL query mode after export"
    )
    parser.add_argument(
        "--dump-all", "-d", action="store_true",
        help="Dump all table contents after export"
    )
    parser.add_argument(
        "--scenes", action="store_true",
        help="Decode and export all HomeKit scenes (human-readable + JSON + CSV)"
    )
    parser.add_argument(
        "--rooms", action="store_true",
        help="Export all accessory→room mappings (human-readable + JSON + CSV)"
    )
    parser.add_argument(
        "--backup-all", action="store_true",
        help="Full backup: export databases + scenes + room mappings"
    )
    parser.add_argument(
        "--output-dir", "-o", type=Path, default=DEFAULT_OUTPUT,
        help=f"Output directory (default: {DEFAULT_OUTPUT})"
    )
    parser.add_argument(
        "--schema-only", "-s", action="store_true",
        help="Only show schema summary, skip full export"
    )
    args = parser.parse_args()

    print(f"\n{'='*70}")
    print(color("  HomeKit Database Export & Inspector", "1;37"))
    print(f"{'='*70}\n")

    check_platform()
    check_fda()

    db_files = discover_databases()
    if not db_files:
        err("No databases found to export.")
        sys.exit(1)
    info(f"Found {len(db_files)} database(s): {', '.join(f.name for f in db_files)}")

    if args.schema_only:
        for db in db_files:
            print_schema_summary(db)
        return

    exported = export_databases(db_files, args.output_dir)
    if not exported:
        err("No databases were successfully exported.")
        sys.exit(1)

    export_dir = exported[0].parent

    print(f"\n{'─'*70}")
    ok(f"Export complete: {export_dir}")
    print(f"{'─'*70}")

    # Summary
    for db in exported:
        try:
            print_schema_summary(db)
        except (sqlite3.OperationalError, sqlite3.DatabaseError) as e:
            warn(f"Could not read schema for {db.name}: {e}")

    # Resolve --backup-all
    do_scenes = args.scenes or args.backup_all
    do_rooms = args.rooms or args.backup_all

    # Scenes mode — reads from LIVE source for reliable data
    if do_scenes:
        core_db = find_core_db(db_files)  # Use live source
        if not core_db:
            err("core.sqlite not found — cannot extract scenes.")
        else:
            info(f"Extracting scenes from {core_db} ...")
            try:
                scenes = extract_scenes(core_db)
                print_scenes(scenes)
                export_scenes_json(scenes, export_dir)
                export_scenes_csv(scenes, export_dir)
                print()
                info("Scene export files:")
                info(f"  JSON: {export_dir / 'scenes_export.json'}")
                info(f"  CSV:  {export_dir / 'scenes_export.csv'}")
                info("")
                info("These files are keyed by accessory NAME + ROOM,")
                info("not internal IDs — they'll survive a Hue Bridge re-pair.")
            except Exception as e:
                err(f"Scene extraction failed: {e}")
                import traceback
                traceback.print_exc()

    # Room mappings mode — reads from LIVE source
    if do_rooms:
        core_db = find_core_db(db_files)
        if not core_db:
            err("core.sqlite not found — cannot extract room mappings.")
        else:
            info(f"Extracting accessory→room mappings from {core_db} ...")
            try:
                room_mappings = extract_accessory_rooms(core_db)
                print_accessory_rooms(room_mappings)
                export_accessory_rooms_json(room_mappings, export_dir)
                export_accessory_rooms_csv(room_mappings, export_dir)
                print()
                info("Room mapping export files:")
                info(f"  JSON: {export_dir / 'accessory_rooms.json'}")
                info(f"  CSV:  {export_dir / 'accessory_rooms.csv'}")
                info("")
                info("Use with: homeclaw-cli assign-rooms --file accessory_rooms.json")
            except Exception as e:
                err(f"Room mapping extraction failed: {e}")
                import traceback
                traceback.print_exc()

    if args.backup_all:
        print(f"\n{'='*70}")
        print(color("  FULL BACKUP COMPLETE", "1;32"))
        print(f"{'='*70}")
        info(f"Export directory: {export_dir}")
        info("")
        info("Restore workflow after re-pairing:")
        info("  1. Re-add Hue Bridge to HomeKit")
        info(f"  2. homeclaw-cli assign-rooms --file {export_dir / 'accessory_rooms.json'}")
        info(f"  3. homeclaw-cli import-scene --file {export_dir / 'scenes_export.json'}")
        print()

    if args.dump_all:
        for db in exported:
            dump_all_tables(db)

    if args.interactive:
        interactive_mode(exported)
    elif not args.dump_all and not do_scenes and not do_rooms:
        print(f"\n  Tip: Re-run with --backup-all for a complete backup,")
        print(f"       --scenes for scene definitions,")
        print(f"       --rooms for accessory→room mappings,")
        print(f"       or --interactive to query the data.\n")


if __name__ == "__main__":
    main()