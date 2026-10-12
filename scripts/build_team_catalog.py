#!/usr/bin/env python3
"""Build and validate the versioned team search catalogue.

The checked-in source snapshot is deliberately separate from the generated
catalogue.  Refreshing sources is a curator action; normal builds and checks are
deterministic and do not need network access.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
SOURCE_PATH = DATA_DIR / "team-catalog.sources.json"
CATALOG_PATH = DATA_DIR / "team-catalog.json"
MANIFEST_PATH = DATA_DIR / "team-assets.manifest.json"
ASSET_DIR = DATA_DIR / "team-assets"
SCHEMA_VERSION = "1.1.0"
CANVAS_SIZE = (512, 512)
UEFA_COMPETITION_IDS = {
    "uefa-champions-league",
    "uefa-europa-league",
    "uefa-conference-league",
}


def normalise(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = "".join(char for char in value if not unicodedata.combining(char))
    value = value.casefold().replace("&", " and ")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", value).split())


def slugify(value: str) -> str:
    return normalise(value).replace(" ", "-")


def initial_for(value: str) -> str:
    key = normalise(value)
    if not key:
        return "#"
    first = key[0].upper()
    return first if "A" <= first <= "Z" else "#"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )


def build_catalog(source: dict[str, Any]) -> dict[str, Any]:
    competitions = source["competitions"]
    entities: list[dict[str, Any]] = []

    for record in source["entities"]:
        entity = dict(record)
        names = [entity["name"], *entity.get("aliases", [])]
        phrases = {normalise(name) for name in names if normalise(name)}
        generic_tokens = {
            "club",
            "de",
            "del",
            "el",
            "fc",
            "cf",
            "cd",
            "ud",
            "rc",
            "real",
            "the",
        }
        tokens = {
            token
            for phrase in phrases
            for token in phrase.split()
            if len(token) >= 3 and token not in generic_tokens
        }
        if entity.get("fifa_code"):
            tokens.add(entity["fifa_code"].casefold())
        terms = sorted(phrases | tokens)
        entity["slug"] = entity.get("slug") or slugify(entity["name"])
        entity["search_terms"] = terms
        entity["search_key"] = " | ".join(terms)
        entity["initial"] = initial_for(entity["name"])
        entities.append(entity)

    # Keep search terms broad and lossless, but make the materialized search key
    # a stable identity key.  Qualifiers are added only when two entities share
    # exactly the same normalized terms (for example the men's and women's
    # CP Cacereño teams).
    by_base_search_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entity in entities:
        by_base_search_key[entity["search_key"]].append(entity)
    for base_key, collisions in by_base_search_key.items():
        if len(collisions) == 1:
            continue
        qualifier_sets = [
            lambda item: f"country={normalise(item.get('country') or 'unknown')}",
            lambda item: (
                f"country={normalise(item.get('country') or 'unknown')}; "
                f"gender={normalise(item.get('gender') or 'unknown')}"
            ),
            lambda item: (
                f"country={normalise(item.get('country') or 'unknown')}; "
                f"gender={normalise(item.get('gender') or 'unknown')}; "
                f"squad={normalise(item.get('squad') or item['id'])}"
            ),
        ]
        for make_qualifier in qualifier_sets:
            qualifiers = [make_qualifier(item) for item in collisions]
            if len(qualifiers) == len(set(qualifiers)):
                for entity, qualifier in zip(collisions, qualifiers, strict=True):
                    entity["search_key"] = f"{base_key} || {qualifier}"
                break

    entities.sort(key=lambda item: (normalise(item["name"]), item["id"]))
    by_initial: dict[str, list[str]] = defaultdict(list)
    by_competition: dict[str, list[str]] = defaultdict(list)
    by_term: dict[str, list[str]] = defaultdict(list)
    for entity in entities:
        by_initial[entity["initial"]].append(entity["id"])
        by_competition[entity["competition_id"]].append(entity["id"])
        for term in entity["search_terms"]:
            by_term[term].append(entity["id"])

    requested_note = (
        "The original request mentioned four Primera Federación groups. The "
        "official 2026/27 competition has two groups of 20, so this catalogue "
        "records the two officially active groups."
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "catalog_id": "la-goyniela-team-catalog-2026-27",
        "season": "2026/27",
        "snapshot_date": source["snapshot_date"],
        "generated_at": source["generated_at"],
        "requested_scope_note": requested_note,
        "asset_policy": {
            "directory": "data/team-assets",
            "format": "PNG",
            "canvas": {"width": 512, "height": 512, "mode": "RGBA"},
            "layout": "flat",
        },
        "competitions": competitions,
        "entities": entities,
        "indexes": {
            "by_initial": dict(sorted(by_initial.items())),
            "by_competition": dict(sorted(by_competition.items())),
            "by_search_term": dict(sorted(by_term.items())),
        },
        "counts": {
            "entities": len(entities),
            "clubs": sum(item["type"] == "club" for item in entities),
            "national_teams": sum(item["type"] == "national_team" for item in entities),
            "by_competition": {
                key: len(value) for key, value in sorted(by_competition.items())
            },
        },
    }


def build_manifest(catalog: dict[str, Any]) -> dict[str, Any]:
    entries = []
    by_id = {item["id"]: item for item in catalog["entities"]}
    for entity_id in sorted(by_id):
        entity = by_id[entity_id]
        path = ROOT / entity["image_path"]
        with Image.open(path) as image:
            width, height = image.size
            mode = image.mode
        entries.append(
            {
                "entity_id": entity_id,
                "file": entity["image_path"],
                "sha256": sha256(path),
                "bytes": path.stat().st_size,
                "width": width,
                "height": height,
                "mode": mode,
                "asset_type": entity["image"]["asset_type"],
                "source_url": entity["image"]["source_url"],
                "source_page_url": entity["image"].get("source_page_url"),
                "source_project": entity["image"]["source_project"],
                "license": entity["image"]["license"],
                "rights_status": entity["image"]["rights_status"],
                "provenance": entity.get("provenance", {}),
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": catalog["generated_at"],
        "asset_root": "data/team-assets",
        "entries": entries,
    }


def _download(url: str) -> bytes:
    last_error: Exception | None = None
    for attempt in range(4):
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": "LaGoynielaCatalog/1.0"}
            )
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read()
        except Exception as exc:  # pragma: no cover - network-only refresh path
            last_error = exc
            time.sleep(attempt + 1)
    assert last_error is not None
    raise last_error


def _download_first(urls: list[str]) -> tuple[bytes, str]:
    errors: list[str] = []
    for url in urls:
        try:
            raw = _download(url)
            if not raw:
                raise ValueError("empty response")
            return raw, url
        except Exception as exc:  # pragma: no cover - network-only refresh path
            errors.append(f"{url}: {exc}")
    raise RuntimeError("all asset sources failed: " + "; ".join(errors))


def normalize_asset(
    original: Image.Image, *, asset_type: str, entity_id: str
) -> Image.Image:
    """Place one asset on the canonical transparent canvas.

    Club source files may contain their own transparent padding. Cropping to
    the alpha bounds before resizing makes the visible crest, rather than the
    source canvas, occupy the normalized 448-pixel contain box. Flag sources
    retain their source aspect and use the existing 448x336 contain box.
    """

    image = original.convert("RGBA")
    alpha_box = image.getchannel("A").getbbox()
    if alpha_box is None:
        raise ValueError(f"asset rendered fully transparent: {entity_id}")
    if asset_type != "flag":
        image = image.crop(alpha_box)

    bounds = (448, 336) if asset_type == "flag" else (448, 448)
    scale = min(bounds[0] / image.width, bounds[1] / image.height)
    resized = (
        max(1, round(image.width * scale)),
        max(1, round(image.height * scale)),
    )
    image = image.resize(resized, Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", CANVAS_SIZE, (0, 0, 0, 0))
    canvas.alpha_composite(
        image,
        ((CANVAS_SIZE[0] - image.width) // 2, (CANVAS_SIZE[1] - image.height) // 2),
    )
    if canvas.getchannel("A").getbbox() is None:
        raise ValueError(f"asset rendered fully transparent: {entity_id}")
    if canvas.getpixel((0, 0))[3] != 0:
        raise ValueError(f"asset has no transparent margin: {entity_id}")
    return canvas


def refresh_assets(
    source: dict[str, Any],
    *,
    missing_only: bool = False,
    competition_ids: set[str] | None = None,
) -> None:
    """Download and normalize every pinned asset source.

    `ffmpeg` is used for the MIT SVG flag sources because its librsvg renderer
    handles clip paths consistently. Club sources are raster images and are
    decoded by Pillow. White pixels inside a crest are never made transparent.
    """

    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    entities = [
        entity
        for entity in source["entities"]
        if competition_ids is None or entity.get("competition_id") in competition_ids
    ]
    for index, entity in enumerate(entities, start=1):
        output_path = ROOT / entity["image_path"]
        if missing_only and output_path.is_file():
            continue
        urls = [
            entity["image"]["source_url"],
            *entity["image"].get("fallback_source_urls", []),
        ]
        raw, url = _download_first(urls)
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            if url.casefold().split("?", 1)[0].endswith(".svg"):
                svg_path = temp / "source.svg"
                raster_path = temp / "source.png"
                svg_path.write_bytes(raw)
                subprocess.run(
                    [
                        "ffmpeg",
                        "-loglevel",
                        "error",
                        "-y",
                        "-i",
                        str(svg_path),
                        "-vf",
                        "scale=640:480",
                        str(raster_path),
                    ],
                    check=True,
                )
            else:
                raster_path = temp / "source-image"
                raster_path.write_bytes(raw)
            with Image.open(raster_path) as original:
                if original.format not in {"PNG", "SVG"}:
                    raise ValueError(
                        f"unexpected asset format {original.format!r}: {entity['id']} ({url})"
                    )
                canvas = normalize_asset(
                    original,
                    asset_type=entity["image"]["asset_type"],
                    entity_id=entity["id"],
                )
                canvas.save(output_path, "PNG", optimize=True)
        if index % 25 == 0:
            print(f"normalized {index}/{len(entities)} assets")


def validate(catalog: dict[str, Any], manifest: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    entities = catalog.get("entities", [])
    ids = [item.get("id") for item in entities]
    paths = [item.get("image_path") for item in entities]
    if len(entities) != 423:
        errors.append(f"expected 423 entities, got {len(entities)}")
    if sum(item.get("type") == "club" for item in entities) != 212:
        errors.append("expected exactly 212 clubs")
    if sum(item.get("type") == "national_team" for item in entities) != 211:
        errors.append("expected exactly 211 FIFA national teams")
    expected = {
        "laliga-ea-sports": 20,
        "laliga-hypermotion": 22,
        "primera-federacion": 40,
        "liga-f": 16,
        "primera-federacion-femenina": 14,
        "fifa-member-associations": 211,
        "uefa-champions-league": 31,
        "uefa-europa-league": 34,
        "uefa-conference-league": 35,
    }
    actual = catalog.get("counts", {}).get("by_competition", {})
    if actual != expected:
        errors.append(f"competition counts differ: expected {expected}, got {actual}")
    if len(ids) != len(set(ids)):
        errors.append("entity IDs are not unique")
    if len(paths) != len(set(paths)):
        errors.append("image paths are not unique")
    search_keys = [item.get("search_key") for item in entities]
    if len(search_keys) != len(set(search_keys)):
        errors.append("search keys are not unique")
    clubs = [item for item in entities if item.get("type") == "club"]
    if any(not item.get("country") for item in clubs):
        errors.append("every club must have a country")
    if any(not item.get("association_code") for item in clubs):
        errors.append("every club must have an association code")
    uefa_ids = [item.get("uefa_team_id") for item in clubs if item.get("uefa_team_id")]
    if len(uefa_ids) != 108 or len(uefa_ids) != len(set(uefa_ids)):
        errors.append("expected exactly 108 unique UEFA team IDs")
    by_id = {item["id"]: item for item in entities}
    expected_uefa_created = {
        "uefa-champions-league": 31,
        "uefa-europa-league": 34,
        "uefa-conference-league": 35,
    }
    for competition_id, expected_created in expected_uefa_created.items():
        metadata = catalog.get("competitions", {}).get(competition_id, {})
        created = [
            item for item in entities if item.get("competition_id") == competition_id
        ]
        deduplicated = metadata.get("deduplicated_existing", [])
        if (
            metadata.get("league_phase_participants") != 36
            or metadata.get("created_identity_count") != expected_created
            or len(created) != expected_created
            or len(deduplicated) != 36 - expected_created
        ):
            errors.append(f"invalid UEFA participant accounting: {competition_id}")
            continue
        accounted_ids = {item.get("uefa_team_id") for item in created}
        for reference in deduplicated:
            entity = by_id.get(reference.get("entity_id"))
            if not entity or entity.get("uefa_team_id") != reference.get("uefa_team_id"):
                errors.append(
                    f"invalid UEFA deduplication reference: {competition_id} "
                    f"{reference.get('entity_id')}"
                )
            accounted_ids.add(reference.get("uefa_team_id"))
        if len(accounted_ids) != 36 or None in accounted_ids:
            errors.append(f"UEFA participant IDs are incomplete: {competition_id}")
    expected_order = sorted(entities, key=lambda item: (normalise(item["name"]), item["id"]))
    if entities != expected_order:
        errors.append("entities are not alphabetically ordered")

    manifest_entries = {item["entity_id"]: item for item in manifest.get("entries", [])}
    if set(manifest_entries) != set(ids):
        errors.append("manifest entity set differs from catalogue")
    for entity in entities:
        path = ROOT / entity["image_path"]
        if not path.is_file():
            errors.append(f"missing image: {entity['image_path']}")
            continue
        try:
            with Image.open(path) as image:
                if image.format != "PNG" or image.size != CANVAS_SIZE or image.mode != "RGBA":
                    errors.append(
                        f"invalid image {entity['image_path']}: "
                        f"{image.format} {image.size} {image.mode}"
                    )
                elif image.getbbox() is None:
                    errors.append(f"fully transparent image: {entity['image_path']}")
                else:
                    alpha_box = image.getchannel("A").getbbox()
                    has_margin = alpha_box and (
                        alpha_box[0] > 0
                        and alpha_box[1] > 0
                        and alpha_box[2] < CANVAS_SIZE[0]
                        and alpha_box[3] < CANVAS_SIZE[1]
                    )
                    if not has_margin:
                        errors.append(
                            f"image lacks a transparent margin: {entity['image_path']}"
                        )
        except OSError as exc:
            errors.append(f"unreadable image {entity['image_path']}: {exc}")
            continue
        manifest_item = manifest_entries.get(entity["id"])
        if manifest_item and manifest_item.get("sha256") != sha256(path):
            errors.append(f"hash mismatch: {entity['image_path']}")

    rebuilt = build_catalog(read_json(SOURCE_PATH))
    if rebuilt != catalog:
        errors.append("catalogue is stale; run scripts/build_team_catalog.py")

    expected_initial: dict[str, list[str]] = defaultdict(list)
    expected_comp: dict[str, list[str]] = defaultdict(list)
    expected_terms: dict[str, list[str]] = defaultdict(list)
    for entity in entities:
        expected_initial[initial_for(entity["name"])].append(entity["id"])
        expected_comp[entity["competition_id"]].append(entity["id"])
        for term in entity["search_terms"]:
            expected_terms[term].append(entity["id"])
    expected_indexes = {
        "by_initial": dict(sorted(expected_initial.items())),
        "by_competition": dict(sorted(expected_comp.items())),
        "by_search_term": dict(sorted(expected_terms.items())),
    }
    if catalog.get("indexes") != expected_indexes:
        errors.append("materialized indexes are incoherent")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="validate without writing")
    parser.add_argument(
        "--refresh-assets",
        action="store_true",
        help="download all pinned sources and regenerate normalized PNG assets",
    )
    parser.add_argument(
        "--refresh-missing-assets",
        action="store_true",
        help="download only absent pinned assets, then regenerate the catalogue",
    )
    parser.add_argument(
        "--refresh-uefa-assets",
        action="store_true",
        help="download and regenerate only UEFA league-phase club assets",
    )
    args = parser.parse_args()
    if not SOURCE_PATH.exists():
        print(f"missing source snapshot: {SOURCE_PATH}", file=sys.stderr)
        return 2

    if args.check:
        if not CATALOG_PATH.exists() or not MANIFEST_PATH.exists():
            print("catalogue or manifest is missing", file=sys.stderr)
            return 1
        errors = validate(read_json(CATALOG_PATH), read_json(MANIFEST_PATH))
        if errors:
            for error in errors:
                print(f"ERROR: {error}", file=sys.stderr)
            return 1
        print("team catalogue check passed: 423 entities and 423 normalized assets")
        return 0

    source = read_json(SOURCE_PATH)
    refresh_modes = sum(
        (args.refresh_assets, args.refresh_missing_assets, args.refresh_uefa_assets)
    )
    if refresh_modes > 1:
        parser.error("choose only one asset refresh mode")
    if args.refresh_assets:
        refresh_assets(source)
    elif args.refresh_missing_assets:
        refresh_assets(source, missing_only=True)
    elif args.refresh_uefa_assets:
        refresh_assets(source, competition_ids=UEFA_COMPETITION_IDS)
    catalog = build_catalog(source)
    write_json(CATALOG_PATH, catalog)
    manifest = build_manifest(catalog)
    write_json(MANIFEST_PATH, manifest)
    print(
        f"wrote {CATALOG_PATH.relative_to(ROOT)} and "
        f"{MANIFEST_PATH.relative_to(ROOT)} ({len(catalog['entities'])} entities)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
