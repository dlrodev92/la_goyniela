from __future__ import annotations

import hashlib
import json
import unittest
from collections import defaultdict
from pathlib import Path

from PIL import Image

from scripts.build_team_catalog import (
    UEFA_COMPETITION_IDS,
    build_catalog,
    initial_for,
    normalise,
    normalize_asset,
)


ROOT = Path(__file__).resolve().parents[1]


class TeamCatalogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = json.loads((ROOT / "data/team-catalog.json").read_text(encoding="utf-8"))
        cls.manifest = json.loads(
            (ROOT / "data/team-assets.manifest.json").read_text(encoding="utf-8")
        )
        cls.entities = cls.catalog["entities"]

    def test_expected_counts(self) -> None:
        self.assertEqual(423, len(self.entities))
        self.assertEqual(212, sum(item["type"] == "club" for item in self.entities))
        self.assertEqual(211, sum(item["type"] == "national_team" for item in self.entities))
        self.assertEqual(
            {
                "laliga-ea-sports": 20,
                "laliga-hypermotion": 22,
                "primera-federacion": 40,
                "liga-f": 16,
                "primera-federacion-femenina": 14,
                "fifa-member-associations": 211,
                "uefa-champions-league": 31,
                "uefa-europa-league": 34,
                "uefa-conference-league": 35,
            },
            self.catalog["counts"]["by_competition"],
        )

    def test_identity_keys_paths_and_fifa_codes_are_unique(self) -> None:
        self.assertEqual(len(self.entities), len({item["id"] for item in self.entities}))
        self.assertEqual(len(self.entities), len({item["image_path"] for item in self.entities}))
        self.assertEqual(len(self.entities), len({item["search_key"] for item in self.entities}))
        codes = [item["fifa_code"] for item in self.entities if item["type"] == "national_team"]
        self.assertEqual(211, len(set(codes)))

    def test_uefa_participants_and_club_countries_are_complete(self) -> None:
        clubs = [item for item in self.entities if item["type"] == "club"]
        self.assertTrue(all(item.get("country") for item in clubs))
        self.assertTrue(all(item.get("association_code") for item in clubs))
        uefa_ids = [item["uefa_team_id"] for item in clubs if item.get("uefa_team_id")]
        self.assertEqual(108, len(uefa_ids))
        self.assertEqual(108, len(set(uefa_ids)))
        expected_created = {
            "uefa-champions-league": 31,
            "uefa-europa-league": 34,
            "uefa-conference-league": 35,
        }
        for competition_id, created in expected_created.items():
            metadata = self.catalog["competitions"][competition_id]
            self.assertEqual(36, metadata["league_phase_participants"])
            self.assertEqual(created, metadata["created_identity_count"])
            self.assertEqual(36 - created, len(metadata["deduplicated_existing"]))
            accounted = {
                item["uefa_team_id"] for item in self.entities
                if item.get("competition_id") == competition_id
            } | {
                item["uefa_team_id"] for item in metadata["deduplicated_existing"]
            }
            self.assertEqual(36, len(accounted))

    def test_images_and_manifest(self) -> None:
        manifest = {item["entity_id"]: item for item in self.manifest["entries"]}
        self.assertEqual({item["id"] for item in self.entities}, set(manifest))
        for entity in self.entities:
            path = ROOT / entity["image_path"]
            self.assertTrue(path.is_file(), entity["image_path"])
            with Image.open(path) as image:
                self.assertEqual("PNG", image.format, entity["image_path"])
                self.assertEqual((512, 512), image.size, entity["image_path"])
                self.assertEqual("RGBA", image.mode, entity["image_path"])
                self.assertIsNotNone(image.getbbox(), entity["image_path"])
                self.assertEqual(0, image.getpixel((0, 0))[3], entity["image_path"])
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            self.assertEqual(digest, manifest[entity["id"]]["sha256"])

    def test_uefa_primary_assets_fill_normalized_contain_box(self) -> None:
        uefa_entities = [
            item
            for item in self.entities
            if item["competition_id"] in UEFA_COMPETITION_IDS
        ]
        self.assertEqual(100, len(uefa_entities))
        for entity in uefa_entities:
            with Image.open(ROOT / entity["image_path"]) as image:
                alpha_box = image.getchannel("A").getbbox()
                self.assertIsNotNone(alpha_box, entity["image_path"])
                assert alpha_box is not None
                width = alpha_box[2] - alpha_box[0]
                height = alpha_box[3] - alpha_box[1]
                self.assertIn(max(width, height), (447, 448), entity["image_path"])

    def test_normalize_asset_rejects_fully_transparent_images(self) -> None:
        transparent = Image.new("RGBA", (240, 240), (0, 0, 0, 0))
        with self.assertRaisesRegex(ValueError, "fully transparent"):
            normalize_asset(
                transparent,
                asset_type="crest",
                entity_id="club-men-transparent",
            )

    def test_entities_are_alphabetical_and_indexes_are_lossless(self) -> None:
        expected_order = sorted(
            self.entities, key=lambda item: (normalise(item["name"]), item["id"])
        )
        self.assertEqual(expected_order, self.entities)
        by_initial: dict[str, list[str]] = defaultdict(list)
        by_competition: dict[str, list[str]] = defaultdict(list)
        by_term: dict[str, list[str]] = defaultdict(list)
        for entity in self.entities:
            self.assertEqual(initial_for(entity["name"]), entity["initial"])
            by_initial[entity["initial"]].append(entity["id"])
            by_competition[entity["competition_id"]].append(entity["id"])
            for term in entity["search_terms"]:
                by_term[term].append(entity["id"])
        self.assertEqual(dict(sorted(by_initial.items())), self.catalog["indexes"]["by_initial"])
        self.assertEqual(
            dict(sorted(by_competition.items())), self.catalog["indexes"]["by_competition"]
        )
        self.assertEqual(dict(sorted(by_term.items())), self.catalog["indexes"]["by_search_term"])

    def test_alias_collisions_are_not_lossy(self) -> None:
        # Duplicate display names are legitimate across men's/women's/reserve teams.
        # The search index must retain every matching canonical entity, never overwrite one.
        expected: dict[str, set[str]] = defaultdict(set)
        for entity in self.entities:
            for raw in [entity["name"], *entity["aliases"]]:
                expected[normalise(raw)].add(entity["id"])
        for term, ids in expected.items():
            indexed = set(self.catalog["indexes"]["by_search_term"][term])
            self.assertTrue(ids <= indexed, f"lossy search term: {term}")

    def test_cacereno_search_key_collision_is_disambiguated(self) -> None:
        ids = {
            "club-men-cp-cacereno",
            "club-women-cp-cacereno",
        }
        entities = [item for item in self.entities if item["id"] in ids]
        self.assertEqual(2, len(entities))
        self.assertEqual(2, len({item["search_key"] for item in entities}))
        for entity in entities:
            self.assertIn("cacereno", entity["search_terms"])
            self.assertIn("cp cacereno", entity["search_terms"])
        self.assertEqual(ids, set(self.catalog["indexes"]["by_search_term"]["cacereno"]))
        self.assertEqual(ids, set(self.catalog["indexes"]["by_search_term"]["cp cacereno"]))

    def test_generated_catalog_is_reproducible(self) -> None:
        source = json.loads(
            (ROOT / "data/team-catalog.sources.json").read_text(encoding="utf-8")
        )
        self.assertEqual(self.catalog, build_catalog(source))


if __name__ == "__main__":
    unittest.main()
