# Stormworks Copy Detector

A local command line tool for comparing a Stormworks vehicle XML with **the Workshop files you have indexed**. Python 3.10+ is the only runtime dependency. The index and Steam downloads persist across scans. A browser page is optional.

## Quick start

From this folder:

```powershell
py -3 -m stormcopy --db workshop.sqlite index-dir "C:\Program Files (x86)\Steam\steamapps\workshop\content\573090"
py -3 -m stormcopy --db workshop.sqlite scan
```

The first `index-dir` run checks every local Workshop XML and can take tens of minutes for a large collection. It shows a progress bar in Command Prompt, including the item being processed. You may press Ctrl+C and rerun the same command later; completed files stay in `workshop.sqlite` and are skipped when unchanged. Later `scan` commands use that saved index and do not repeat the full indexing step.

If this database was created by an earlier detector version, build the MinHash/LSH and local-fingerprint search data for files that are still on disk:

```bat
py -3 -m stormcopy --db workshop.sqlite upgrade-index
```

The upgrade commits files in small batches and resumes after the last committed batch when rerun. `upgrade-index --max-files 100` limits one run to 100 available files. `status` shows how many files have the current MinHash/LSH fingerprints. Missing source XML stays in the older searchable index; if you obtain it again, rerun `index-dir` on its folder or rerun the upgrade. A newly downloaded or indexed XML gets the new search data automatically.

While an index or download run is active, open another Command Prompt in this folder and run `py -3 -m stormcopy --db workshop.sqlite status` to see indexed vehicles, cached items, and the remaining known download queue.

## Fill your existing Steam Workshop folder

The `download-workshop` command searches public Stormworks Workshop metadata, reuses item folders already present in your Steam installation, and downloads accessible missing or outdated items into that **same** folder. It saves its discovery cursor and download state in `workshop.sqlite`, so you can stop with Ctrl+C and rerun it. SteamCMD runs from a separate staging folder.

In **Command Prompt**, from this project folder:

```bat
py -3 -m stormcopy setup-steamcmd --cache steam-cache
steam-cache\steamcmd.exe +login anonymous +quit
set "STEAM_API_KEY=your personal Steam Web API key"
py -3 -m stormcopy --db workshop.sqlite download-workshop --workshop-folder "C:\Program Files (x86)\Steam\steamapps\workshop\content\573090" --cache steam-cache
```

The existing Workshop folder is detected automatically in the standard Steam location, but `--workshop-folder` lets you choose another Steam library. The folder must already exist and end in `steamapps\workshop\content\573090`. Leave the API key out of the command line itself. If anonymous SteamCMD access fails for an item, sign in to SteamCMD with an account that can access the content and add `--login ACCOUNT_NAME` to the last command.

By default this follows the full public Workshop metadata cursor before downloading, then tries every accessible queued item in bounded groups of 50. Known file sizes are sorted smallest first; Steam does not provide a global size-sorted Workshop query, and items without reported sizes come afterward. A full run can take a long time and a lot of storage. Use `--pages 10 --max-items 100` for a trial, then rerun without those limits to continue. After a full discovery pass has finished, use `--restart-discovery` later to find newly published items. The command keeps 20 GB free by default; adjust with `--reserve-free-gb 40`, for example. For faster downloads, try `--workers 8 --delay 0`, subject to Steam's limits. The progress bar and final report show discovery coverage, existing folders, downloaded items, failures, and the remaining queue. Folder modification times are used as a freshness estimate for already installed items.

To fill only IDs already in your database without an API key, use `download-workshop --known-only`. To defer fingerprinting until the files are present, add `--download-only`, then run:

```bat
py -3 -m stormcopy --db workshop.sqlite index-dir "C:\Program Files (x86)\Steam\steamapps\workshop\content\573090"
```

That indexing step also picks up existing Steam folders that were not yet in the search index. SteamCMD and the Steam API can only reach items your account may access; private, removed, or unavailable items will remain missing. A completed public metadata pass does not guarantee that the whole Workshop was downloaded. The downloader backs off on reported rate limits and gives failed items a retry delay.

At the prompt, paste the path to the XML file or drag the file into Command Prompt, then press Enter. You can also provide the path directly:

```powershell
py -3 -m stormcopy --db workshop.sqlite scan "C:\path\to\vehicle.xml"
```

The file is scanned against the index; it is not added as a Workshop reference. The Command Prompt report shows the overlap status, best matching item and Workshop link, directional structure similarity, MinHash estimate, heuristic confidence, and example matching positions in readable text. Use `--json` after a command if another program needs the structured result, for example `scan "C:\path\to\vehicle.xml" --json`. Progress stays on the error stream so JSON output remains parseable. Colours appear in a compatible interactive terminal; `--no-color` or the `NO_COLOR` environment variable turns them off. Nothing is uploaded to a remote server. If you prefer the optional local browser page, run `py -3 -m stormcopy --db workshop.sqlite serve` and open `http://127.0.0.1:8765/`.

To cache Workshop files, install Valve's [SteamCMD](https://developer.valvesoftware.com/wiki/SteamCMD). On Windows the CLI can download its bootstrap into a chosen cache folder:

```bat
py -3 -m stormcopy setup-steamcmd --cache "C:\steamcmd"
```

Run `C:\steamcmd\steamcmd.exe +login anonymous +quit` once to let SteamCMD self-update. The downloader saves Workshop files in that cache and indexes any vehicle XML it finds. It sorts **known items by Steam's reported primary file size, smallest first**, with unknown sizes last. Known item sizes are looked up automatically before each download run; this lookup does not need an API key.

For items you already know about (including items indexed from your local Steam folder), run:

```powershell
py -3 -m stormcopy --db workshop.sqlite download --cache "C:\steamcmd" --max-items 100 --batch-size 10
```

For a faster **download-first** pass, run several isolated SteamCMD workers and skip fingerprinting until the cache is populated:

```bat
py -3 -m stormcopy --db workshop.sqlite download --cache "C:\steamcmd" --max-items 100 --batch-size 10 --workers 8 --delay 0 --cache-only
py -3 -m stormcopy --db workshop.sqlite index-dir "C:\steamcmd\steamapps\workshop\content\573090"
```

The second command builds the searchable index from the cached XML. `--cache-only` clears old fingerprints for items it replaces, so run `index-dir` before scanning again. Use `--max-items 0` if you intend to process every known queued item in one run; the whole Workshop can require substantial time and storage.

## Find and cache items by Workshop tag

First list the exact tags attached to your known Workshop items. The first refresh uses Steam's public details endpoint and needs no API key:

```bat
py -3 -m stormcopy --db workshop.sqlite tags --refresh
py -3 -m stormcopy --db workshop.sqlite tags --match Air
```

Use repeated `--tag` options to require **all** of those tags. Add `--exclude-tag` to narrow the set, or `--match-any` if any required tag is enough. For example:

```bat
py -3 -m stormcopy --db workshop.sqlite download --cache steam-cache --tag Vehicle --tag Air --exclude-tag Wip --max-items 100 --workers 8 --delay 0 --cache-only
py -3 -m stormcopy --db workshop.sqlite index-dir "steam-cache\steamapps\workshop\content\573090"
```

Without a Steam Web API key, the downloader refreshes missing or week-old tags in 50-item public batches, selects matching **known items**, then downloads the smallest reported files first. Private or removed IDs are retried after a day instead of on every run. `tags --force` rechecks every known item's tags immediately. With `STEAM_API_KEY` set, a filtered download first discovers one more matching Workshop page, then refreshes missing or week-old tags for known items before selecting downloads. Each exact filter has its own saved cursor. Add `--discover-pages 10` to search ten pages first, `--discover-pages 0` for the remaining matching pages, or `--known-only` to use only IDs already in the database. You can also run `discover --tag Vehicle --tag Air --exclude-tag Wip --pages 10` separately. Steam tag names should match the published tags exactly for Workshop search; `tags` shows names already seen in your index. Tag filtering does not guarantee an item contains a vehicle XML.

On Windows, `download` now starts up to four SteamCMD workers by default. Each worker has its own SteamCMD files and Workshop manifest under `C:\steamcmd\.stormcopy-workers`; completed items are moved into the usual `C:\steamcmd\steamapps\workshop\content\573090` folder. Worker setup uses extra disk space once, but downloaded item folders are moved rather than copied. SteamCMD must be installed directly in the selected cache folder for parallel mode. Use `--workers 1` for a SteamCMD executable elsewhere, or if your Steam account rejects concurrent sessions. `--workers` accepts 1–8. More workers may increase download speed when one SteamCMD session cannot use your connection; compare a bounded run with four and eight workers if the network remains idle. Steam does not publish a guaranteed download rate or safe parallel-session limit, so actual speed depends on Steam, your account, disk, and connection. The downloader backs off and reduces active workers if Steam explicitly reports a rate limit.

SteamCMD starts once per batch of ten items, and the default delay is 0.5 seconds between batch starts. `--batch-size` accepts 1–50, and `--delay 0` removes the extra pause. The result reports elapsed time and workers used. Runs are resumable: successful items are not downloaded again unless their Workshop update time changes. Failed items get a retry cooldown so they do not block the rest of the queue. Only one downloader can use a cache at a time. `--max-items 0` processes every queued item. To redownload one item, use `--item-id 1234567890 --force`. Start with a bounded batch to check disk space and access. SteamCMD may require an account with access to Stormworks content; use `--login ACCOUNT_NAME` if anonymous access fails and sign in to SteamCMD interactively first. Run SteamCMD once on its own before the downloader, since its first launch may self-update and exit early. If a Steam installation already holds the items, `index-dir` can index those files without downloading them again.

To discover more public item IDs at scale, provide a personal Steam Web API key via the `STEAM_API_KEY` environment variable. From **Command Prompt**:

```bat
set "STEAM_API_KEY=your key"
py -3 -m stormcopy --db workshop.sqlite discover --pages 100
py -3 -m stormcopy --db workshop.sqlite refresh-sizes
py -3 -m stormcopy --db workshop.sqlite download --cache "C:\steamcmd" --max-items 100 --batch-size 10
```

`discover` saves the next cursor, so running it again resumes. `--pages 0` follows the cursor until the metadata pass ends; this can be a long run. `--restart` starts a new pass. `refresh-sizes` fills missing reported sizes for known IDs without an API key. HTTP 429 and transient API errors back off and retry. SteamCMD failures are recorded in SQLite. Keep the API key private and do not serve the page outside localhost.

For later maintenance, run `discover --sort updated --restart --pages 10` in batches to see recently changed items. Each sort has its own saved cursor. Steam offers no Workshop query sorted by size: the detector sorts only the IDs discovered so far, and the size is the primary published file's size rather than a guaranteed total installed folder size. Complete public-item coverage requires finishing the full `published` cursor pass and downloading every accessible vehicle item; private, removed, and inaccessible items remain outside the index. A complete download can require substantial time and storage.

## What a match means

The detector extracts Stormworks `<c>` components with `<vp>` grid positions. It sorts XML attributes and unordered child records, normalizes decimal spellings, ignores regenerated IDs and cosmetic paint/name fields, and keeps bodies separate so two bodies at the same local coordinate do not form a false structure. It hashes component orientation/content and each radius-two neighborhood. It also builds **spatial winnowing** fingerprints from contiguous component runs along each grid axis. These runs are ordered by coordinates, so rearranging XML elements does not break them. Logic links use relative endpoint positions; substantial microprocessor scripts and internal graphs add independent evidence.

Each file gets a 64-value **MinHash** sketch with 16 **LSH** buckets for close overall matches. A bounded index of rare neighborhood, winnowing, logic, and microprocessor hashes retrieves smaller copied sections that whole-vehicle LSH can miss. A scan merges those candidates, then loads compressed full fingerprints and compares exact multiset counts before ranking. New indexes store full fingerprints once per file in compressed form and keep only a bounded sample of hashes as lookup rows, limiting database growth. The saved index and download cache are reused across scans.

The reported **similarity percentage** is the share of the submitted vehicle's neighborhood instances found in the Workshop vehicle; `workshop_coverage_percent` measures the reverse. `combined_similarity_percent` is a heuristic blend of available geometry, winnowing, logic, and microprocessor coverage. The MinHash estimate is approximate whole-vehicle set Jaccard and can be low even when a real 20% section matches. Evidence includes matching neighborhood and sequence positions; a cluster of nearby rare matches can raise a partial-copy result to medium suspicion. A high label still requires broad rare structural overlap.

Some genuine Stormworks files use numeric XML attribute names (`00="1"`) and repeated attributes. The parser repairs those names in memory for fingerprinting; it never changes Workshop files on disk. Nonvehicle XML such as standalone microcontrollers is counted as ignored. Rerunning `index-dir` on an existing folder upgrades its vehicle files to the new search data and adds previously skipped vehicles.

Confidence is a **heuristic**, not a probability or finding of plagiarism. Generic shapes, common templates, and independently built geometry can match. The tool deliberately says “no strong match in indexed set” when nothing significant is found. The report includes indexed item/file counts, MinHash/LSH upgrade coverage, and whether API discovery reached its end; it cannot establish originality against private, deleted, inaccessible, or unindexed items. Winnowing needs a shared contiguous component run to guarantee a selected hash; the neighborhood index provides a second route for shorter or interrupted assemblies.

## Steam access and coverage

Steam's [QueryFiles API](https://partner.steamgames.com/doc/webapi/IPublishedFileService) supports public Workshop metadata queries with cursor pagination (deeper than the 1,000-page `page` limit), but requires an API key. It is not a bulk vehicle-file endpoint. [Steam Workshop's item installation API](https://partner.steamgames.com/doc/api/isteamugc) and SteamCMD retrieve item content subject to account/game access. Stormworks Workshop content is stored in the Steam `steamapps/workshop/content/573090/<item id>` tree; addons may contain several `vehicle_*.xml` files. This project indexes every usable vehicle XML in each item. Some items may contain no vehicle XML or may not be downloadable by your account. Steam publishes no guarantee that one account can enumerate and download the entire Workshop, so full historical coverage must be measured rather than assumed.

## Validation

```powershell
py -3 -m unittest discover -s tests -v
```

The tests check XML order/metadata/translation tolerance, copied subassemblies, unrelated geometry, and unsafe XML rejection. Live Steam discovery/download requires your key and Steam access and is not part of the offline test suite.
