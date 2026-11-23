# 🥡 takeaway

> Your places, to go.

You spent years starring cafés, saving "Want to go" spots and pinning that one ghost town you'll *definitely* visit someday. **takeaway** packs them all up from a [Google Takeout](https://takeout.google.com) export so you can carry them to whichever map app you're moving to.

```
🥡  Google Takeout  ──▶  takeaway  ──▶  📍 GeoJSON   📍 KML   📍 GPX
```

- **One file per list.** "Want to go" stays "Want to go". Two lists with the same name get a ` (2)` suffix instead of overwriting each other.
- **Finds the coordinates Google leaves out.** Saved lists only contain links, so takeaway works out where each place actually is.
- **No dependencies.** Just Python 3.9+.
- **Re-runs are instant.** Lookups are cached.

## Where can my places go?

| Format | Good for |
| --- | --- |
| `geojson` | HERE WeGo, geojson.io, QGIS, most web maps |
| `kml` | Organic Maps, CoMaps, OsmAnd, Google Earth |
| `gpx` | OsmAnd, Organic Maps, GPS devices, hiking apps |

## 1. Order your takeout 🛍️

At [takeout.google.com](https://takeout.google.com), click **Deselect all**, then tick:

- **Saved**: your lists (Want to go, Favorites, custom lists)
- **Maps (your places)**: your starred places
- **Maps** *(optional)*: labeled places like Home and Work

Download the zip when Google emails you.

## 2. Take it away 🚶

```sh
python3 takeaway.py ~/Downloads/takeout-XXXX.zip --resolve --geocode --format kml,gpx
```

The input can be the `.zip` or the extracted folder. Your places land in `output/`:

```
output/
├── Want to go.kml
├── Want to go.gpx
├── Starred places.kml
├── Starred places.gpx
└── ...
```

Then import the files into your new map app. 🎉

## How it finds your places 🔍

Google's saved list CSVs only contain a link per place, usually without coordinates. takeaway tries, in order:

1. **Reading the link**: dropped pins and some places carry their coordinates in the URL.
2. **`--resolve`**: loading the Google Maps page and reading where the map is centered.
3. **`--geocode`**: searching the place name on [OpenStreetMap Nominatim](https://nominatim.org), politely, at 1 request per second.

Anything that still can't be found is listed at the end, so nothing goes missing silently. Lists where nothing could be found are reported too, and no file is written for them.

## Options

| Option | What it does |
| --- | --- |
| `--format LIST` | Comma-separated output formats: `geojson`, `kml`, `gpx` (default `geojson`) |
| `--out DIR` | Output folder (default `output/` next to the script) |
| `--resolve` | Fetch Google Maps pages to find coordinates |
| `--geocode` | Fall back to Nominatim geocoding |
| `--chunk N` | Split each list into files of at most N places, for apps that choke on big imports (default `0`, no splitting) |
| `--no-url` | Leave Google Maps links out of the output |
| `--cache FILE` | Where to keep the lookup cache (default `cache.json` next to the script) |

## Privacy 🔒

takeaway runs entirely on your machine, except:

- `--resolve` sends place links to Google (who already know them).
- `--geocode` sends place names and addresses to OpenStreetMap Nominatim.

`output/` and `cache.json` contain your places and are git-ignored.

## Development 🛠️

```sh
python3 -m unittest discover tests   # no dependencies needed
pip install -e '.[dev]'              # ruff + mypy, for linting and type checking
ruff check . && ruff format --check .
mypy
```

The tests don't use the network: online lookups go through a fake fetcher. CI runs the tests on every supported Python, plus ruff and mypy.

## AI assisted

This app was built with the help of Claude, the code was read and checked by Deepseek instance and by me for errors/safety/readability/maintainability/clarity.

## License

MIT, see [LICENSE](LICENSE).
