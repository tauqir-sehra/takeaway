"""Tests for takeaway.py. Run with: python3 -m unittest discover tests

No network access: online lookups go through a fake fetcher, and the
politeness/rate-limit delays are patched to zero.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import takeaway  # noqa: E402

KML_NS = {"k": "http://www.opengis.net/kml/2.2"}
GPX_NS = {"g": "http://www.topografix.com/GPX/1/1"}

# A list with a description above the header, like Google exports it.
LIST_WITH_DESCRIPTION_CSV = """﻿Places I liked

Title,Note,URL,Tags,Comment
,,,,
Golden Gate Park,nice & green,https://www.google.com/maps/place/Golden+Gate+Park/data=!4m2!3m1!1s0x1:0x2!3d37.7694!4d-122.4862,,
Dropped pin,,"https://www.google.com/maps/search/37.80,+-122.41",,
No coordinates,,https://www.google.com/maps/place/Foo/data=!4m2!3m1!1s0x3:0x4,,
"""

PLAIN_LIST_CSV = """Title,Note,URL,Tags,Comment
,,,,
Café <Tom's>,,"https://www.google.com/maps/search/40.1,+-74.2",,
"""

STARRED_GEOJSON = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [-122.4194, 37.7749]},
            "properties": {
                "google_maps_url": "http://maps.google.com/?cid=1",
                "location": {"name": "City Hall", "address": "SF, CA"},
                "Comment": "pretty",
            },
        },
        {  # [0, 0] means unknown; the URL has the real coordinates.
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [0, 0]},
            "properties": {"google_maps_url": "http://maps.google.com/?q=40.7,-74.0", "location": {"name": "NYC"}},
        },
    ],
}

LABELED_GEOJSON = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [-73.9, 40.6]},
            "properties": {"name": "Home", "address": "1 Main St"},
        }
    ],
}


def make_takeout_zip(directory: Path, extra: dict[str, str] | None = None) -> Path:
    path = directory / "takeout.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Takeout/Saved/Liked.csv", LIST_WITH_DESCRIPTION_CSV)
        archive.writestr("Takeout/Saved/Plain.csv", PLAIN_LIST_CSV)
        archive.writestr("Takeout/Maps (your places)/Saved Places.json", json.dumps(STARRED_GEOJSON))
        archive.writestr("Takeout/Maps/My labeled places/Labeled places.json", json.dumps(LABELED_GEOJSON))
        archive.writestr("Takeout/Maps/Commute settings/Commute settings.json", "{}")  # Ignored.
        for name, text in (extra or {}).items():
            archive.writestr(name, text)
    return path


def run_takeaway(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(ROOT / "takeaway.py"), *args], capture_output=True, text=True)


class FakeFetcher:
    """Stands in for takeaway.http_get: canned responses chosen by URL prefix."""

    def __init__(self, responses: dict[str, tuple[str, str]]):
        self.responses = responses
        self.calls: list[str] = []

    def __call__(self, url: str, headers: dict[str, str]) -> tuple[str, str]:
        self.calls.append(url)
        for prefix, response in self.responses.items():
            if url.startswith(prefix):
                return response
        raise OSError(f"no canned response for {url}")


def find(element: ET.Element, path: str, namespace: dict[str, str]) -> ET.Element:
    """Element.find() that fails the test loudly instead of returning None."""
    found = element.find(path, namespace)
    if found is None:
        raise AssertionError(f"missing XML element: {path}")
    return found


class CoordinateParsingTest(unittest.TestCase):
    def test_url_patterns(self) -> None:
        cases = {
            "https://www.google.com/maps/place/X/data=!4m2!3m1!1s0x1:0x2!3d37.5!4d-122.25": (37.5, -122.25),
            "https://www.google.com/maps/place/X/@37.5,-122.25,15z": (37.5, -122.25),
            "https://www.google.com/maps/search/37.5,+-122.25": (37.5, -122.25),
            "http://maps.google.com/?q=37.5%2C-122.25": (37.5, -122.25),
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(takeaway.find_coordinates(url, takeaway.URL_COORDINATE_PATTERNS), expected)

    def test_no_coordinates(self) -> None:
        url = "https://www.google.com/maps/place/X/data=!4m2!3m1!1s0x89c25ba680cf167d:0xae707cb333dd80d8"
        self.assertIsNone(takeaway.find_coordinates(url, takeaway.URL_COORDINATE_PATTERNS))

    def test_zero_and_out_of_range_are_invalid(self) -> None:
        self.assertFalse(takeaway.is_valid_coordinate(0, 0))
        self.assertFalse(takeaway.is_valid_coordinate(91, 0))
        self.assertTrue(takeaway.is_valid_coordinate(0, 1))


class GeometryTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # Every online source is off, so a lookup can only ever return "not found".
        cache = takeaway.LookupCache(Path(tmp.name) / "cache.json")
        self.locator = takeaway.Locator(cache, resolve=False, geocode=False)

    def parse(self, geometry: object) -> takeaway.Collection:
        document = {"features": [{"geometry": geometry, "properties": {"location": {"name": "X"}}}]}
        return takeaway.parse_geojson_places(json.dumps(document), "x", self.locator)

    def test_three_values_use_the_first_two(self) -> None:
        collection = self.parse({"type": "Point", "coordinates": [-122.4, 37.7, 12.0]})
        self.assertEqual((collection.places[0].lat, collection.places[0].lng), (37.7, -122.4))

    def test_missing_or_empty_geometry_is_reported_missing(self) -> None:
        cases: list[object] = [None, {}, {"coordinates": []}, {"coordinates": "nope"}]
        for geometry in cases:
            with self.subTest(geometry=geometry):
                collection = self.parse(geometry)
                self.assertEqual(collection.places, [])
                self.assertEqual(collection.missing, ["X"])

    def test_invalid_json_is_an_error(self) -> None:
        with self.assertRaises(takeaway.TakeoutError):
            takeaway.parse_geojson_places("{", "x", self.locator)


class ChunkingTest(unittest.TestCase):
    def test_split(self) -> None:
        places = list(range(7))
        self.assertEqual(takeaway.split_into_chunks(places, 0), [places])
        self.assertEqual(takeaway.split_into_chunks(places, 10), [places])
        self.assertEqual([len(c) for c in takeaway.split_into_chunks(places, 3)], [3, 3, 1])
        self.assertEqual(takeaway.split_into_chunks([], 3), [[]])


class CsvParsingTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cache = takeaway.LookupCache(Path(tmp.name) / "cache.json")
        self.locator = takeaway.Locator(cache, resolve=False, geocode=False)

    def test_missing_header_is_an_error(self) -> None:
        with self.assertRaises(takeaway.TakeoutError):
            takeaway.parse_saved_list_csv("Name,Link\nA,B\n", "x", self.locator)

    def test_description_row_and_empty_row_are_skipped(self) -> None:
        collection = takeaway.parse_saved_list_csv(LIST_WITH_DESCRIPTION_CSV, "Liked", self.locator)
        self.assertEqual([p.name for p in collection.places], ["Golden Gate Park", "Dropped pin"])
        self.assertEqual(collection.places[0].description, "nice & green")
        self.assertEqual(collection.missing, ["No coordinates"])

    def test_progress_callback_reports_each_found_place(self) -> None:
        seen: list[tuple[str, str]] = []

        def collect(name: str, method: str) -> None:
            seen.append((name, method))

        takeaway.parse_saved_list_csv(PLAIN_LIST_CSV, "Plain", self.locator, collect)
        self.assertEqual(seen, [("Café <Tom's>", "url")])


class LookupCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "cache.json"

    def test_round_trip(self) -> None:
        cache = takeaway.LookupCache(self.path)
        self.assertEqual(cache.cached("k", lambda: (1.5, 2.5)), (1.5, 2.5))
        cache.save()
        reloaded = takeaway.LookupCache(self.path)
        self.assertEqual(reloaded.cached("k", lambda: self.fail("should come from the cache")), (1.5, 2.5))

    def test_not_found_is_cached_as_null(self) -> None:
        cache = takeaway.LookupCache(self.path)
        self.assertIsNone(cache.cached("k", lambda: None))
        self.assertIsNone(cache.cached("k", lambda: self.fail("should come from the cache")))
        cache.save()
        self.assertEqual(json.loads(self.path.read_text()), {"k": None})

    def test_failures_are_not_cached(self) -> None:
        def failing() -> None:
            raise OSError("network down")

        cache = takeaway.LookupCache(self.path)
        with self.assertRaises(OSError):
            cache.cached("k", failing)
        self.assertNotIn("k", cache.entries)

    def test_corrupt_cache_is_ignored(self) -> None:
        self.path.write_text('{"k": [1,')
        with mock.patch("sys.stderr"):
            cache = takeaway.LookupCache(self.path)
        self.assertEqual(cache.entries, {})

    def test_save_leaves_no_temporary_file(self) -> None:
        cache = takeaway.LookupCache(self.path)
        cache.cached("k", lambda: (1.0, 2.0))
        cache.save()
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["cache.json"])


class LocatorTest(unittest.TestCase):
    GOOGLE_PLACE = "https://www.google.com/maps/place/Foo/data=!4m2!3m1!1s0x1:0x2"

    def setUp(self) -> None:
        for name in ("GOOGLE_DELAY_SECONDS", "NOMINATIM_MIN_INTERVAL_SECONDS"):
            patcher = mock.patch.object(takeaway, name, 0)
            patcher.start()
            self.addCleanup(patcher.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cache = takeaway.LookupCache(Path(tmp.name) / "cache.json")

    def locator(self, fetcher: FakeFetcher) -> takeaway.Locator:
        return takeaway.Locator(self.cache, resolve=True, geocode=True, fetch=fetcher)

    def test_url_wins_without_any_fetch(self) -> None:
        fetcher = FakeFetcher({})
        coordinates, method = self.locator(fetcher).locate("X", "https://x/@37.5,-122.25,15z")
        self.assertEqual((coordinates, method), ((37.5, -122.25), "url"))
        self.assertEqual(fetcher.calls, [])

    def test_google_redirect_url(self) -> None:
        fetcher = FakeFetcher({self.GOOGLE_PLACE: ("https://x/@37.5,-122.25,15z", "")})
        self.assertEqual(self.locator(fetcher).locate("Foo", self.GOOGLE_PLACE), ((37.5, -122.25), "google"))

    def test_google_page_center(self) -> None:
        page = '<meta content="https://maps.google.com/maps/api/staticmap?center=40.7%2C-74.0&zoom=15">'
        fetcher = FakeFetcher({self.GOOGLE_PLACE: (self.GOOGLE_PLACE, page)})
        self.assertEqual(self.locator(fetcher).locate("Foo", self.GOOGLE_PLACE), ((40.7, -74.0), "google"))

    def test_nominatim_tries_address_before_name(self) -> None:
        body = json.dumps([{"lat": "10.0", "lon": "20.0"}])
        fetcher = FakeFetcher({takeaway.NOMINATIM_URL: ("", body)})
        coordinates, method = self.locator(fetcher).locate("Some Cafe", "", "1 Main St")
        self.assertEqual((coordinates, method), ((10.0, 20.0), "nominatim"))
        self.assertEqual(len(fetcher.calls), 1)
        self.assertIn("q=1+Main+St", fetcher.calls[0])

    def test_second_lookup_comes_from_the_cache(self) -> None:
        fetcher = FakeFetcher({takeaway.NOMINATIM_URL: ("", json.dumps([{"lat": "10.0", "lon": "20.0"}]))})
        locator = self.locator(fetcher)
        self.assertEqual(locator.locate("Some Cafe"), ((10.0, 20.0), "nominatim"))
        self.assertEqual(locator.locate("Some Cafe"), ((10.0, 20.0), "nominatim"))
        self.assertEqual(len(fetcher.calls), 1)

    def test_network_failure_is_reported_not_raised(self) -> None:
        fetcher = FakeFetcher({})  # Every request raises OSError.
        with mock.patch("sys.stderr"):
            self.assertEqual(self.locator(fetcher).locate("Foo", self.GOOGLE_PLACE), (None, "not found"))
        self.assertEqual(self.cache.entries, {})


class TakeoutKindTest(unittest.TestCase):
    def test_known_files(self) -> None:
        saved_list = takeaway.kind_for("Takeout/Saved/Liked.csv")
        assert saved_list is not None
        self.assertIsNone(saved_list.collection)  # Named after the file stem.

        starred = takeaway.kind_for("Takeout/Maps (your places)/Saved Places.json")
        assert starred is not None
        self.assertEqual(starred.collection, "Starred places")

    def test_unrelated_files_are_ignored(self) -> None:
        self.assertIsNone(takeaway.kind_for("Takeout/Maps/Commute settings/Commute settings.json"))
        self.assertIsNone(takeaway.kind_for("Takeout/Saved/notes.txt"))
        # CSVs are only read from the Saved folder.
        self.assertIsNone(takeaway.kind_for("Takeout/Elsewhere/Liked.csv"))


class EndToEndTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.takeout = make_takeout_zip(self.dir)
        self.out = self.dir / "out"
        self.cache = self.dir / "cache.json"

    def run_ok(self, *extra: str) -> str:
        result = run_takeaway(str(self.takeout), "--out", str(self.out), "--cache", str(self.cache), *extra)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_geojson_default(self) -> None:
        stdout = self.run_ok()
        self.assertEqual(
            sorted(p.name for p in self.out.iterdir()),
            ["Labeled places.geojson", "Liked.geojson", "Plain.geojson", "Starred places.geojson"],
        )
        liked = json.loads((self.out / "Liked.geojson").read_text())
        self.assertEqual([f["properties"]["name"] for f in liked["features"]], ["Golden Gate Park", "Dropped pin"])
        self.assertEqual(liked["features"][0]["geometry"]["coordinates"], [-122.4862, 37.7694])  # lng, lat
        self.assertIn("No coordinates", stdout)  # Reported as missing.
        self.assertIn("1 MISSING", stdout)
        self.assertIn("Labeled places: 1 place", stdout)  # Singular when it's just one.

        starred = json.loads((self.out / "Starred places.geojson").read_text())
        self.assertEqual(starred["features"][1]["geometry"]["coordinates"], [-74.0, 40.7])
        self.assertEqual(starred["features"][0]["properties"]["description"], "pretty")

        labeled = json.loads((self.out / "Labeled places.geojson").read_text())
        self.assertEqual(labeled["features"][0]["properties"]["address"], "1 Main St")

    def test_kml(self) -> None:
        self.run_ok("--format", "kml")
        self.assertFalse(list(self.out.glob("*.geojson")))
        root = ET.parse(self.out / "Liked.kml").getroot()
        self.assertEqual(find(root, "k:Document/k:name", KML_NS).text, "Liked")
        placemarks = root.findall(".//k:Placemark", KML_NS)
        self.assertEqual(len(placemarks), 2)
        self.assertEqual(placemarks[0].find("k:coordinates", KML_NS), None)
        self.assertEqual(find(placemarks[0], "k:Point/k:coordinates", KML_NS).text, "-122.4862,37.7694")
        description = find(placemarks[0], "k:description", KML_NS).text or ""
        self.assertTrue(description.startswith("nice & green\nhttps://www.google.com/maps/place/"))

        # Special characters survive XML escaping.
        plain = ET.parse(self.out / "Plain.kml").getroot()
        self.assertEqual(find(plain, ".//k:Placemark/k:name", KML_NS).text, "Café <Tom's>")

    def test_gpx(self) -> None:
        self.run_ok("--format", "gpx")
        root = ET.parse(self.out / "Starred places.gpx").getroot()
        self.assertEqual(root.get("version"), "1.1")
        waypoints = root.findall("g:wpt", GPX_NS)
        self.assertEqual(
            [(w.get("lat"), w.get("lon")) for w in waypoints], [("37.7749", "-122.4194"), ("40.7", "-74.0")]
        )
        first = waypoints[0]
        # GPX schema order: name, desc, link.
        self.assertEqual([child.tag.split("}")[1] for child in first], ["name", "desc", "link"])
        self.assertEqual(find(first, "g:desc", GPX_NS).text, "pretty\nSF, CA")
        self.assertEqual(find(first, "g:link", GPX_NS).get("href"), "http://maps.google.com/?cid=1")

    def test_multiple_formats_chunk_and_no_url(self) -> None:
        self.run_ok("--format", "geojson,kml,gpx", "--chunk", "1", "--no-url")
        names = sorted(p.name for p in self.out.iterdir())
        for ext in (".geojson", ".kml", ".gpx"):
            self.assertIn("Liked 1 of 2" + ext, names)
            self.assertIn("Liked 2 of 2" + ext, names)
            self.assertIn("Plain" + ext, names)  # Single place: not numbered.
        self.assertNotIn("http", (self.out / "Liked 1 of 2.gpx").read_text().split("<wpt", 1)[1])
        geojson = json.loads((self.out / "Liked 1 of 2.geojson").read_text())
        self.assertNotIn("url", geojson["features"][0]["properties"])

    def test_empty_list_writes_no_file(self) -> None:
        takeout = make_takeout_zip(self.dir, {"Takeout/Saved/Empty list.csv": "Title,Note,URL,Tags,Comment\n,,,,\n"})
        result = run_takeaway(str(takeout), "--out", str(self.out), "--cache", str(self.cache))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(list(self.out.glob("Empty list.*")), [])
        self.assertIn("Empty list: 0 places", result.stdout)

    def test_duplicate_list_names_get_distinct_files(self) -> None:
        takeout = make_takeout_zip(
            self.dir,
            {
                "Takeout/Saved/A/Starred.csv": 'Title,Note,URL,Tags,Comment\nOne,,"https://www.google.com/maps/search/1.0,2.0",,\n',
                "Takeout/Saved/B/Starred.csv": 'Title,Note,URL,Tags,Comment\nTwo,,"https://www.google.com/maps/search/3.0,4.0",,\n',
            },
        )
        result = run_takeaway(str(takeout), "--out", str(self.out), "--cache", str(self.cache))
        self.assertEqual(result.returncode, 0, result.stderr)
        # The fixture also has a "Starred places" list, hence the three names.
        self.assertEqual(
            sorted(p.name for p in self.out.glob("Starred*.geojson")),
            ["Starred (2).geojson", "Starred places.geojson", "Starred.geojson"],
        )
        names = [
            feature["properties"]["name"]
            for path in (self.out / "Starred.geojson", self.out / "Starred (2).geojson")
            for feature in json.loads(path.read_text())["features"]
        ]
        self.assertEqual(sorted(names), ["One", "Two"])

    def test_corrupt_cache_does_not_stop_the_run(self) -> None:
        self.cache.write_text('{"g:http://x": [1,')
        result = run_takeaway(str(self.takeout), "--out", str(self.out), "--cache", str(self.cache))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ignoring unreadable cache", result.stderr)
        self.assertEqual(len(list(self.out.glob("*.geojson"))), 4)

    def test_folder_input(self) -> None:
        folder = self.dir / "extracted"
        with zipfile.ZipFile(self.takeout) as archive:
            archive.extractall(folder)
        result = run_takeaway(str(folder), "--out", str(self.out), "--cache", str(self.cache))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(list(self.out.glob("*.geojson"))), 4)

    def test_bad_format(self) -> None:
        result = run_takeaway(str(self.takeout), "--format", "kml,shp")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown format shp", result.stderr)

    def test_negative_chunk_is_rejected(self) -> None:
        result = run_takeaway(str(self.takeout), "--chunk", "-1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must be 0 (no splitting) or more", result.stderr)

    def test_takeout_without_saved_places(self) -> None:
        empty = self.dir / "empty.zip"
        with zipfile.ZipFile(empty, "w") as archive:
            archive.writestr("Takeout/Maps/Commute settings/Commute settings.json", "{}")
        result = run_takeaway(str(empty), "--out", str(self.out), "--cache", str(self.cache))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Re-export in Takeout", result.stderr)

    def test_missing_input_is_an_error(self) -> None:
        missing = self.dir / "nope.zip"
        result = run_takeaway(str(missing), "--out", str(self.out), "--cache", str(self.cache))
        self.assertEqual(result.returncode, 1)
        self.assertIn("Not a Takeout .zip file or folder", result.stderr)
        self.assertFalse(self.out.exists())


if __name__ == "__main__":
    unittest.main()
