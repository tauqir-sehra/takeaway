#!/usr/bin/env python3
"""takeaway: get your Google Maps saved places out of a Google Takeout export.

Each saved list becomes one file per output format:

  geojson   GeoJSON FeatureCollection (HERE WeGo, geojson.io, QGIS, ...)
  kml       KML Document (Organic Maps, CoMaps, OsmAnd, Google Earth, ...)
  gpx       GPX waypoints (OsmAnd, Organic Maps, GPS devices, ...)

Supported Takeout files (from a .zip or an extracted folder):

  Takeout/Maps (your places)/Saved Places.json   starred places (GeoJSON)
  Takeout/Maps/My labeled places/Labeled places.json   Home, Work, ... (GeoJSON)
  Takeout/Saved/<list name>.csv                  custom lists (CSV)

The CSV lists only contain a Google Maps URL per place, usually without
coordinates. Coordinates are found by trying, in order:

  1. Parsing the URL itself (works for dropped pins and some place links).
  2. --resolve: downloading the Google Maps page and reading the map center.
  3. --geocode: searching the place name on OpenStreetMap Nominatim.

Online lookups are cached in cache.json next to this script (see --cache), so reruns are fast.

Example:
  python3 takeaway.py ~/Downloads/takeout-XXXX.zip --resolve --geocode --format kml,gpx
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeVar

# A (latitude, longitude) pair in decimal degrees.
LatLng = tuple[float, float]

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = SCRIPT_DIR / "output"
DEFAULT_CACHE_PATH = SCRIPT_DIR / "cache.json"

# Decimals kept in written coordinates: ~1 cm, far beyond what Takeout knows.
COORDINATE_DECIMALS = 7

# Identifies this script to OpenStreetMap Nominatim, as its usage policy requires.
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_USER_AGENT = "takeaway/1.1"
NOMINATIM_MIN_INTERVAL_SECONDS = 1.1  # Nominatim allows at most 1 request/second.

# Google serves a stripped-down page without coordinates to unknown clients,
# so we look like a regular desktop browser when resolving Google Maps URLs.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
)
GOOGLE_DELAY_SECONDS = 0.5  # Be polite to Google between page fetches.
HTTP_TIMEOUT_SECONDS = 20


class TakeoutError(Exception):
    """The input cannot be converted: not a Takeout export, or an unreadable file."""


def warn(message: str) -> None:
    """Report a non-fatal problem on stderr."""
    print(f"warning: {message}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Finding coordinates in text
# ---------------------------------------------------------------------------

# A decimal number such as "-122.4194". Requires a decimal point so that
# integer IDs in URLs are not mistaken for coordinates.
_NUMBER = r"(-?\d{1,3}\.\d+)"

# Ways Google Maps URLs embed a location, each capturing (lat, lng).
URL_COORDINATE_PATTERNS: Sequence[re.Pattern[str]] = [
    # .../data=!...!3d37.7694!4d-122.4862  (place links with coordinates)
    re.compile(r"!3d" + _NUMBER + r"!4d" + _NUMBER),
    # .../@37.7694,-122.4862,15z  (map viewport)
    re.compile(r"@" + _NUMBER + r"," + _NUMBER),
    # .../maps/search/37.80,+-122.41  (dropped pins)
    re.compile(r"/search/" + _NUMBER + r",\s*\+?" + _NUMBER),
    # ...?q=40.7,-74.0  (query parameters, possibly URL-encoded comma)
    re.compile(r"[?&](?:q|query|ll)=" + _NUMBER + r"(?:,|%2C)\s*\+?" + _NUMBER, re.IGNORECASE),
]

# Ways a Google Maps HTML page reveals the location it is centered on.
PAGE_COORDINATE_PATTERNS: Sequence[re.Pattern[str]] = [
    # <meta content="https://maps.google.com/maps/api/staticmap?center=37.7%2C-122.4...">
    re.compile(r"center=" + _NUMBER + r"(?:%2C|,)" + _NUMBER),
    re.compile(r"@" + _NUMBER + r"," + _NUMBER),
]


def is_valid_coordinate(lat: float, lng: float) -> bool:
    """True if lat/lng are in range. (0, 0) counts as invalid: Takeout uses it for "unknown"."""
    in_range = -90 <= lat <= 90 and -180 <= lng <= 180
    return in_range and (lat, lng) != (0, 0)


def find_coordinates(text: str, patterns: Sequence[re.Pattern[str]]) -> LatLng | None:
    """Return the first valid (lat, lng) matched by any of the patterns, or None."""
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            lat, lng = float(match.group(1)), float(match.group(2))
            if is_valid_coordinate(lat, lng):
                return lat, lng
    return None


# ---------------------------------------------------------------------------
# Online lookups (Google Maps pages and OpenStreetMap Nominatim)
# ---------------------------------------------------------------------------

# What a lookup fetcher does: GET a URL, return (final URL after redirects, body).
HttpFetch = Callable[[str, dict[str, str]], tuple[str, str]]


def http_get(url: str, headers: dict[str, str]) -> tuple[str, str]:
    """GET a URL. Returns (final URL after redirects, response body as text)."""
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
        return response.geturl(), response.read().decode("utf-8", "replace")


def _as_latlng(value: object) -> LatLng | None:
    """Turn a cached [lat, lng] list into a pair, ignoring anything malformed."""
    if isinstance(value, list) and len(value) >= 2:
        lat, lng = value[0], value[1]
        if isinstance(lat, (int, float)) and isinstance(lng, (int, float)):
            return float(lat), float(lng)
    return None


class LookupCache:
    """Persistent cache of online lookups, stored as JSON.

    Keys are "g:<google url>" or "n:<nominatim query>". Values are [lat, lng],
    or null when the lookup succeeded but found nothing (so it isn't retried).
    Failed lookups are never stored, so they are retried on the next run.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.entries: dict[str, list[float] | None] = self._load()

    def cached(self, key: str, fetch: Callable[[], LatLng | None]) -> LatLng | None:
        """Return the cached result for key, calling fetch() once if it is unknown.

        A fetch that fails raises, and its result is not stored.
        """
        if key in self.entries:
            return _as_latlng(self.entries[key])
        coordinates = fetch()
        self.entries[key] = [coordinates[0], coordinates[1]] if coordinates else None
        return coordinates

    def save(self) -> None:
        """Write the cache atomically, so an interrupted run cannot corrupt it."""
        temporary = self.path.with_name(self.path.name + ".tmp")
        try:
            temporary.write_text(json.dumps(self.entries, indent=1), encoding="utf-8")
            os.replace(temporary, self.path)
        except OSError as error:
            warn(f"could not save cache {self.path}: {error}")

    def _load(self) -> dict[str, list[float] | None]:
        """Read the cache, tolerating a missing, corrupt or hand-edited file."""
        try:
            entries = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as error:
            warn(f"ignoring unreadable cache {self.path}: {error}")
            return {}
        return entries if isinstance(entries, dict) else {}


class Locator:
    """Finds coordinates for a place using its Google Maps URL, address or name."""

    def __init__(
        self,
        cache: LookupCache,
        *,
        resolve: bool,
        geocode: bool,
        fetch: HttpFetch = http_get,
    ) -> None:
        self.cache = cache
        self.resolve = resolve
        self.geocode = geocode
        self._fetch = fetch
        self._last_nominatim_request = 0.0

    def locate(self, name: str, url: str = "", address: str = "") -> tuple[LatLng | None, str]:
        """Return (coordinates or None, how they were found)."""
        coordinates = find_coordinates(url, URL_COORDINATE_PATTERNS)
        if coordinates:
            return coordinates, "url"

        if self.resolve and url:
            coordinates = self._from_google(url)
            if coordinates:
                return coordinates, "google"

        if self.geocode:
            # An address is a more precise query than a place name, so try it first.
            for query in (address, name):
                if query:
                    coordinates = self._from_nominatim(query)
                    if coordinates:
                        return coordinates, "nominatim"

        return None, "not found"

    def _from_google(self, url: str) -> LatLng | None:
        """Fetch a Google Maps page and read the coordinates it is centered on."""

        def fetch() -> LatLng | None:
            headers = {"User-Agent": BROWSER_USER_AGENT, "Accept-Language": "en"}
            final_url, page = self._fetch(url, headers)
            time.sleep(GOOGLE_DELAY_SECONDS)
            # Redirects often land on a URL that contains the coordinates.
            return find_coordinates(final_url, URL_COORDINATE_PATTERNS) or find_coordinates(
                page, PAGE_COORDINATE_PATTERNS
            )

        return self._cached("g:" + url, "Google", fetch)

    def _from_nominatim(self, query: str) -> LatLng | None:
        """Geocode a free-text query (name or address) with OpenStreetMap Nominatim."""

        def fetch() -> LatLng | None:
            self._wait_for_nominatim_rate_limit()
            params = urllib.parse.urlencode({"q": query, "format": "json", "limit": 1})
            _, body = self._fetch(f"{NOMINATIM_URL}?{params}", {"User-Agent": NOMINATIM_USER_AGENT})
            results = json.loads(body)
            if not results:
                return None
            return float(results[0]["lat"]), float(results[0]["lon"])

        return self._cached("n:" + query, "Nominatim", fetch)

    def _cached(self, key: str, what: str, fetch: Callable[[], LatLng | None]) -> LatLng | None:
        """Run one cached lookup, reporting network problems instead of crashing."""
        try:
            return self.cache.cached(key, fetch)
        except (OSError, ValueError) as error:
            # OSError covers DNS, timeout and HTTP errors; ValueError covers bad JSON.
            warn(f"{what} lookup failed: {error}")
            return None

    def _wait_for_nominatim_rate_limit(self) -> None:
        elapsed = time.time() - self._last_nominatim_request
        if elapsed < NOMINATIM_MIN_INTERVAL_SECONDS:
            time.sleep(NOMINATIM_MIN_INTERVAL_SECONDS - elapsed)
        self._last_nominatim_request = time.time()


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Place:
    """One saved place, with coordinates already resolved."""

    name: str
    lat: float
    lng: float
    description: str = ""
    address: str = ""
    url: str = ""


@dataclass
class Collection:
    """One saved list, e.g. "Want to go" or "Starred places"."""

    name: str
    places: list[Place] = field(default_factory=list)
    # Names of places whose coordinates couldn't be found (so were left out).
    missing: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Parsing places
# ---------------------------------------------------------------------------

# Called as (place name, how it was found) for each place that needed a lookup.
Progress = Callable[[str, str], None]


def _ignore_progress(*_args: str) -> None:
    """Default Progress: say nothing."""


def _geometry_latlng(geometry: object) -> LatLng | None:
    """Read (lat, lng) from a GeoJSON Point, or None if it holds no usable location.

    Takeout writes [0, 0] for places it could not locate, and some exports add a
    third elevation value, so only the first two numbers are used.
    """
    if not isinstance(geometry, dict):
        return None
    coordinates = geometry.get("coordinates")
    if not isinstance(coordinates, (list, tuple)) or len(coordinates) < 2:
        return None
    try:
        lng, lat = float(coordinates[0]), float(coordinates[1])
    except (TypeError, ValueError):
        return None
    return (lat, lng) if is_valid_coordinate(lat, lng) else None


def parse_geojson_places(text: str, name: str, locator: Locator, progress: Progress = _ignore_progress) -> Collection:
    """Parse "Saved Places.json" or "Labeled places.json".

    Starred places keep their details under properties.location; labeled places
    have name/address directly in properties.
    """
    try:
        document = json.loads(text)
    except ValueError as error:
        raise TakeoutError(f"not valid JSON: {error}") from error
    features = document.get("features") if isinstance(document, dict) else None
    if not isinstance(features, list):
        features = []

    collection = Collection(name)
    for feature in features:
        properties = feature.get("properties") or {}
        location = properties.get("location") or {}

        address = location.get("address") or properties.get("address", "")
        place_name = location.get("name") or properties.get("name") or properties.get("Title") or address or "Unnamed"
        url = properties.get("google_maps_url") or properties.get("Google Maps URL", "")
        note = properties.get("Comment", "")

        coordinates = _geometry_latlng(feature.get("geometry"))
        if coordinates is None:
            coordinates, method = locator.locate(place_name, url, address)
            if coordinates:
                progress(place_name, method)

        if coordinates is None:
            collection.missing.append(place_name)
            continue

        collection.places.append(
            Place(
                name=place_name,
                lat=coordinates[0],
                lng=coordinates[1],
                description=note,
                address=address,
                url=url,
            )
        )
    return collection


def parse_saved_list_csv(text: str, name: str, locator: Locator, progress: Progress = _ignore_progress) -> Collection:
    """Parse a saved list CSV with columns Title, Note, URL, Tags, Comment.

    Lists with a description have it (plus a blank line) above the header row,
    so we skip ahead to the line starting with "Title,".
    """
    lines = text.splitlines(keepends=True)
    header_index = next((i for i, line in enumerate(lines) if line.lstrip().startswith("Title,")), None)
    if header_index is None:
        raise TakeoutError("no 'Title,Note,URL,Tags,Comment' header row found")

    collection = Collection(name)
    rows = csv.DictReader(io.StringIO("".join(lines[header_index:])))
    for row in rows:
        # Malformed rows put extra fields in a list under the None key; ignore those.
        fields = {key.strip(): value.strip() for key, value in row.items() if key and isinstance(value, str)}
        title, url = fields.get("Title", ""), fields.get("URL", "")
        if not title and not url:
            continue  # Google adds an empty row after the header.

        description = "\n".join(
            part for part in (fields.get("Note"), fields.get("Comment"), fields.get("Tags")) if part
        )
        coordinates, method = locator.locate(title, url)
        if coordinates is None:
            collection.missing.append(title or url)
            continue

        progress(title or url, method)
        collection.places.append(
            Place(name=title or "Unnamed", lat=coordinates[0], lng=coordinates[1], description=description, url=url)
        )
    return collection


# ---------------------------------------------------------------------------
# Takeout files
# ---------------------------------------------------------------------------

# Parses one file's text into a Collection.
Parser = Callable[[str, str, Locator, Progress], Collection]


@dataclass(frozen=True)
class TakeoutKind:
    """One kind of Takeout file takeaway understands."""

    suffix: str  # Filename suffix, e.g. ".csv".
    parser: Parser
    collection: str | None = None  # Collection name; None means "use the file stem".
    folder: str | None = None  # Required folder in the export, if any.


# Every file type takeaway reads. The reader and the parsers meet here, so
# adding a format means adding one entry, not editing three functions.
TAKEOUT_FILES: Sequence[TakeoutKind] = (
    TakeoutKind(suffix="Saved Places.json", parser=parse_geojson_places, collection="Starred places"),
    TakeoutKind(suffix="Labeled places.json", parser=parse_geojson_places, collection="Labeled places"),
    TakeoutKind(suffix=".csv", parser=parse_saved_list_csv, folder="Saved"),
)


@dataclass(frozen=True)
class TakeoutFile:
    """One readable file from the export."""

    path: str  # Path inside the zip, or on disk.
    kind: TakeoutKind
    text: str

    @property
    def collection_name(self) -> str:
        """The saved list this file holds, e.g. "Want to go"."""
        return self.kind.collection or Path(self.path).stem


def kind_for(path: str) -> TakeoutKind | None:
    """The Takeout kind matching this path, or None if takeaway can't read it."""
    normalised = "/" + path.replace("\\", "/")
    for kind in TAKEOUT_FILES:
        if not normalised.endswith(kind.suffix):
            continue
        if kind.folder and f"/{kind.folder}/" not in normalised:
            continue
        return kind
    return None


def read_takeout(source: Path) -> Iterator[TakeoutFile]:
    """Yield every supported file from a Takeout .zip or an extracted folder."""
    # "utf-8-sig" strips the byte order mark that Google puts at the start of the CSVs.
    if source.is_file() and zipfile.is_zipfile(source):
        with zipfile.ZipFile(source) as archive:
            for name in archive.namelist():
                kind = kind_for(name)
                if kind:
                    yield TakeoutFile(name, kind, archive.read(name).decode("utf-8-sig", "replace"))
    elif source.is_dir():
        for path in sorted(source.rglob("*")):
            kind = kind_for(str(path)) if path.is_file() else None
            if kind:
                yield TakeoutFile(str(path), kind, path.read_text(encoding="utf-8-sig", errors="replace"))
    else:
        raise TakeoutError(f"Not a Takeout .zip file or folder: {source}")


# ---------------------------------------------------------------------------
# Writing output
# ---------------------------------------------------------------------------


def safe_filename(name: str) -> str:
    """Replace characters that aren't allowed in file names."""
    return re.sub(r'[\\/:*?"<>|]+', "_", name).strip() or "collection"


T = TypeVar("T")


def split_into_chunks(items: Sequence[T], chunk_size: int) -> list[list[T]]:
    """Split items into lists of at most chunk_size (0 = no splitting). Always returns at least one list."""
    if chunk_size <= 0 or len(items) <= chunk_size:
        return [list(items)]
    return [list(items[i : i + chunk_size]) for i in range(0, len(items), chunk_size)]


# Each renderer turns one (possibly chunked) list of places into file contents.
# They all take (collection name, places, include_urls) and return text.
Renderer = Callable[[str, Sequence[Place], bool], str]


def _geojson_feature(place: Place, include_url: bool) -> dict[str, object]:
    """One GeoJSON Feature.

    Both "name" and "title" are set because apps differ in which one they display.
    """
    properties: dict[str, str] = {"name": place.name, "title": place.name}
    if place.description:
        properties["description"] = place.description
    if place.address:
        properties["address"] = place.address
    if place.url and include_url:
        properties["url"] = place.url
    return {
        "type": "Feature",
        # GeoJSON coordinate order is [longitude, latitude].
        "geometry": {
            "type": "Point",
            "coordinates": [round(place.lng, COORDINATE_DECIMALS), round(place.lat, COORDINATE_DECIMALS)],
        },
        "properties": properties,
    }


def render_geojson(name: str, places: Sequence[Place], include_urls: bool) -> str:
    geojson = {
        "type": "FeatureCollection",
        "name": name,
        "features": [_geojson_feature(place, include_urls) for place in places],
    }
    return json.dumps(geojson, ensure_ascii=False, indent=1) + "\n"


def _xml_to_string(root: ET.Element) -> str:
    ET.indent(root, space=" ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"


def _kml_description(place: Place, include_url: bool) -> str:
    """KML has an <address> element but no link field, so the URL goes in the description."""
    parts = [place.description]
    if include_url and place.url:
        parts.append(place.url)
    return "\n".join(part for part in parts if part)


def render_kml(name: str, places: Sequence[Place], include_urls: bool) -> str:
    """KML: one <Document> per list, one <Placemark> per place."""
    kml = ET.Element("kml", xmlns="http://www.opengis.net/kml/2.2")
    document = ET.SubElement(kml, "Document")
    ET.SubElement(document, "name").text = name
    for place in places:
        placemark = ET.SubElement(document, "Placemark")
        ET.SubElement(placemark, "name").text = place.name
        if place.address:
            ET.SubElement(placemark, "address").text = place.address
        description = _kml_description(place, include_urls)
        if description:
            ET.SubElement(placemark, "description").text = description
        point = ET.SubElement(placemark, "Point")
        # KML coordinate order is longitude,latitude (like GeoJSON).
        longitude = round(place.lng, COORDINATE_DECIMALS)
        latitude = round(place.lat, COORDINATE_DECIMALS)
        ET.SubElement(point, "coordinates").text = f"{longitude},{latitude}"
    return _xml_to_string(kml)


def render_gpx(name: str, places: Sequence[Place], include_urls: bool) -> str:
    """GPX 1.1: one <wpt> (waypoint) per place.

    The GPX schema requires child elements in a fixed order: name, desc, link.
    GPX has no address field, so the address goes in <desc>.
    """
    gpx = ET.Element("gpx", version="1.1", creator="takeaway", xmlns="http://www.topografix.com/GPX/1/1")
    metadata = ET.SubElement(gpx, "metadata")
    ET.SubElement(metadata, "name").text = name
    for place in places:
        waypoint = ET.SubElement(
            gpx,
            "wpt",
            lat=str(round(place.lat, COORDINATE_DECIMALS)),
            lon=str(round(place.lng, COORDINATE_DECIMALS)),
        )
        ET.SubElement(waypoint, "name").text = place.name
        description = "\n".join(part for part in (place.description, place.address) if part)
        if description:
            ET.SubElement(waypoint, "desc").text = description
        if include_urls and place.url:
            link = ET.SubElement(waypoint, "link", href=place.url)
            ET.SubElement(link, "text").text = "Google Maps"
    return _xml_to_string(gpx)


# Output format name -> (file extension, renderer).
OUTPUT_FORMATS: dict[str, tuple[str, Renderer]] = {
    "geojson": (".geojson", render_geojson),
    "kml": (".kml", render_kml),
    "gpx": (".gpx", render_gpx),
}


class OutputWriter:
    """Writes collections to disk: one file per list per format."""

    def __init__(
        self,
        directory: Path,
        *,
        formats: Sequence[str],
        chunk_size: int,
        include_urls: bool,
    ) -> None:
        self.directory = directory
        self.formats = formats
        self.chunk_size = chunk_size
        self.include_urls = include_urls
        self._used_names: set[str] = set()

    def write(self, collection: Collection) -> list[Path]:
        """Write every chunk of collection in every format. Returns the paths written.

        A collection with no resolved places writes nothing, rather than an empty
        file that the target app would import as an empty list.
        """
        if not collection.places:
            return []
        self.directory.mkdir(parents=True, exist_ok=True)
        name = self._unique_name(collection.name)
        chunks = split_into_chunks(collection.places, self.chunk_size)
        written: list[Path] = []
        for number, chunk in enumerate(chunks, start=1):
            title = name if len(chunks) == 1 else f"{name} {number} of {len(chunks)}"
            for format_name in self.formats:
                extension, render = OUTPUT_FORMATS[format_name]
                path = self.directory / (safe_filename(title) + extension)
                path.write_text(render(title, chunk, self.include_urls), encoding="utf-8")
                written.append(path)
        return written

    def _unique_name(self, name: str) -> str:
        """A name not used by an earlier list, since two lists can share a name."""
        candidate, suffix = name, 1
        while candidate in self._used_names:
            suffix += 1
            candidate = f"{name} ({suffix})"
        self._used_names.add(candidate)
        return candidate


def _place_count(count: int) -> str:
    return f"{count} place" if count == 1 else f"{count} places"


def print_summary(collections: Sequence[Collection], output_dir: Path, *, tried_everything: bool) -> None:
    print("\nSummary:")
    for collection in collections:
        line = f"  {collection.name}: {_place_count(len(collection.places))}"
        if collection.missing:
            line += f", {len(collection.missing)} MISSING"
        print(line)
        for name in collection.missing:
            print(f"      - {name}")

    if any(collection.missing for collection in collections) and not tried_everything:
        print("\nTip: rerun with --resolve and/or --geocode to locate missing places.")
    print(f"\nOutput: {output_dir}")


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Options:
    """Everything the conversion needs, parsed and validated."""

    takeout: Path
    output_dir: Path
    cache_path: Path
    formats: tuple[str, ...]
    chunk_size: int
    include_urls: bool
    resolve: bool
    geocode: bool


def parse_formats(value: str) -> tuple[str, ...]:
    """Parse "kml,gpx" into ("kml", "gpx"), rejecting unknown formats."""
    formats = [part.strip().lower() for part in value.split(",") if part.strip()]
    unknown = [name for name in formats if name not in OUTPUT_FORMATS]
    if unknown or not formats:
        bad = ", ".join(unknown) or repr(value)
        raise argparse.ArgumentTypeError(f"unknown format {bad}; choose from {', '.join(OUTPUT_FORMATS)}")
    return tuple(dict.fromkeys(formats))  # Drop duplicates, keep order.


def _chunk_size(value: str) -> int:
    """--chunk: 0 means "no splitting", anything else must be a positive count."""
    try:
        size = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"expected a whole number, got {value!r}") from error
    if size < 0:
        raise argparse.ArgumentTypeError(f"must be 0 (no splitting) or more, got {size}")
    return size


def parse_args(argv: Sequence[str] | None = None) -> Options:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("takeout", type=Path, help="Takeout .zip file or extracted folder")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT_DIR, help="output folder (default: %(default)s)")
    parser.add_argument(
        "--cache", type=Path, default=DEFAULT_CACHE_PATH, help="lookup cache file (default: %(default)s)"
    )
    parser.add_argument("--resolve", action="store_true", help="fetch Google Maps pages to find coordinates")
    parser.add_argument("--geocode", action="store_true", help="fall back to OpenStreetMap Nominatim geocoding")
    parser.add_argument(
        "--chunk",
        type=_chunk_size,
        default=0,
        metavar="N",
        help="split collections into files of at most N places (default: 0, no splitting)",
    )
    parser.add_argument(
        "--format",
        type=parse_formats,
        default="geojson",
        metavar="LIST",
        help=f"comma-separated output formats: {', '.join(OUTPUT_FORMATS)} (default: %(default)s)",
    )
    parser.add_argument("--no-url", action="store_true", help="leave Google Maps URLs out of the output")
    args = parser.parse_args(argv)
    return Options(
        takeout=args.takeout.expanduser(),
        output_dir=args.out.expanduser(),
        cache_path=args.cache.expanduser(),
        formats=args.format,
        chunk_size=args.chunk,
        include_urls=not args.no_url,
        resolve=args.resolve,
        geocode=args.geocode,
    )


def report_place(name: str, method: str) -> None:
    """Print one line of progress for a place that needed an online lookup."""
    print(f"    {name[:50]:50} [{method}]")


def _parse_file(takeout_file: TakeoutFile, locator: Locator) -> Collection:
    """Parse one Takeout file, naming the file if it turns out to be unreadable."""
    try:
        return takeout_file.kind.parser(takeout_file.text, takeout_file.collection_name, locator, report_place)
    except TakeoutError as error:
        raise TakeoutError(f"{takeout_file.path}: {error}") from error


def run(options: Options) -> list[Collection]:
    """Convert every supported file in the export, writing output as it goes.

    Raises TakeoutError when the input holds nothing takeaway can convert.
    """
    cache = LookupCache(options.cache_path)
    locator = Locator(cache, resolve=options.resolve, geocode=options.geocode)
    writer = OutputWriter(
        options.output_dir,
        formats=options.formats,
        chunk_size=options.chunk_size,
        include_urls=options.include_urls,
    )

    collections: list[Collection] = []
    try:
        for takeout_file in read_takeout(options.takeout):
            print(f"== {takeout_file.path}")
            collection = _parse_file(takeout_file, locator)
            if not writer.write(collection):
                print("    no places with coordinates, nothing written")
            collections.append(collection)
    finally:
        cache.save()  # Even an interrupted run keeps the lookups it paid for.

    if not collections:
        raise TakeoutError(
            "No 'Saved Places.json', 'Labeled places.json' or 'Saved/*.csv' found in input.\n"
            "Re-export in Takeout with 'Saved' and 'Maps (your places)' selected."
        )
    return collections


def main(argv: Sequence[str] | None = None) -> int:
    options = parse_args(argv)
    try:
        collections = run(options)
    except (TakeoutError, OSError) as error:
        print(error, file=sys.stderr)
        return 1
    print_summary(collections, options.output_dir, tried_everything=options.resolve and options.geocode)
    return 0


if __name__ == "__main__":
    sys.exit(main())
