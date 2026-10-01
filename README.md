# Stormworks Copy Detector

A tool for comparing a Stormworks vehicle XML with **the Workshop files you have indexed locally or in a shared index**. Python 3.10+ is the only runtime dependency. The index and Steam downloads persist across scans. Use the Tkinter window or command line; the shared search service provides a fingerprint API without a browser upload page.

## Quick start

Double-click `start-gui.bat`, or run this from the project folder:

```bat
py -3 -m stormcopy
```

Choose a vehicle XML and click **Compare locally**. The Results tab shows colour-coded certainty and tables for the main match, up to three possible matches, matching data types, matching positions, and program/index information. It shows the percentage of the input vehicle's structure that overlaps, the Workshop link, elapsed comparison time, and the app's current process memory use. The best match's public Workshop description loads in the background when available. The Debug resources tab shows activity and local resource information.

The **3D previews** tab shows the input beside a selected locally stored Workshop match. Choose among reported matches with the **Workshop match** selector. Each vehicle is shown as individual grid cubes in a rotatable 3D view and top, side, and front views. Green marks the same component type at an aligned position, yellow marks a changed type at an aligned position, red marks a position with no corresponding cube, and gray means spatial alignment could not be verified. The alignment uses shared structural evidence from the scan or strong agreement across the complete geometry; these colours are a preview of grid correspondence, not a separate copy verdict. Drag a 3D view to rotate both vehicles or scroll to zoom. The viewer processes every positioned component within the 500,000-component file limit. Each view is one depth-tested image, so hidden interior cubes are naturally occluded; it does not reconstruct Stormworks meshes, moving bodies, or paint. A shared match with no local XML shows an unavailable message.

The **Known Workshop IDs** tab pages through the saved ID catalog 100 at a time and supports exact ID lookup without loading all IDs into memory. **Find ID** looks up one saved number. **Find IDs** traverses Steam's publicly discoverable Stormworks listing, saving each page of IDs and showing the page and saved-ID counts. Click **Stop finding IDs** to pause; another **Find IDs** click resumes the saved cursor. A completed full pass starts from the beginning when requested again. A saved Steam Web API key is required. Automatic discovery also resumes in one-page background steps about once per minute and backs off after errors or a completed refresh. Steam cannot provide private, removed, or inaccessible IDs. New IDs are metadata only until their XML is downloaded and indexed.

The window searches the saved index immediately and reports how many Workshop files are actually searchable. It checks the installed Workshop folder for new files in the background and updates a local comparison after indexing them. Repeated comparisons reuse the saved fingerprints; they do not download the Workshop again. The memory figure is the application's working set after the scan, not a precise allocation for that one comparison.

The certainty column uses **High**, **Medium**, **Low**, **Holds some matched microcontrollers**, **Uncertain**, or **No match found**. These are heuristic labels. **Uncertain** includes an empty or partly upgraded index; **No match found** means no reportable match among the files actually searched. The percentage describes how much of the input vehicle's structure overlaps, not the probability of copying. A controller-only match can have 0% structural overlap while still showing controller evidence.

Steam's Workshop search API does not search inside uploaded XML files. Comparing against every public Workshop vehicle requires first obtaining and indexing each accessible file; the window reports the current local coverage rather than claiming a complete remote search.

For command-line use:

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

## Search a shared fingerprint index

A computer with an indexed Workshop collection can answer comparisons for other computers. The host keeps its `workshop.sqlite` and Workshop files; a client sends structural fingerprints of its chosen XML and receives ranked Workshop matches and coverage counts. The host does not download new items during a search. It reads the current SQLite index for each request, so later indexing runs improve subsequent searches without restarting the service.

On the computer holding the index, double-click `start-search-service.bat` or run this in Command Prompt:

```bat
py -3 -m stormcopy --db workshop.sqlite serve-search
```

This listens only on `127.0.0.1:8766`. It exposes JSON status at `/` and `/v1/coverage`, and accepts structural fingerprint JSON at `/v1/scan`. It has no browser upload page and does not accept raw XML. Use `py -3 -m stormcopy --db workshop.sqlite status` for detailed local index status. Keep the service running while comparing; restart it after updating the program.

In the Tkinter window, choose the vehicle XML, enter `http://127.0.0.1:8766` in **Shared index URL**, and click **Search shared index**. Leave **Access token** empty for an unprotected service running only on your own computer. **Compare locally** still searches the SQLite index on the client's computer. For a service hosted elsewhere, enter its HTTPS URL and access token instead.

To make the index available to another computer, use an HTTPS reverse proxy with a valid certificate and a long secret access token. Set `STORMCOPY_SEARCH_TOKEN` in the host's environment before starting `serve-search`, then configure the client with the HTTPS address and the same token. Keep `serve-search` bound to `127.0.0.1` when the proxy runs on the same computer; if a proxy on another machine must reach it, use `--host` with an appropriate private network address and restrict access to that address. The proxy must forward the `Authorization` header. Do not expose the plain HTTP service directly to the internet. The client accepts plain HTTP only for a loopback address and rejects redirects, so a remote address needs HTTPS from the start.

When searching a shared index from Tkinter, only the XML's structural hashes, counts, and example component positions are sent; the full XML stays on the client. Fingerprints still reveal some of the creation's structure, so choose a shared server deliberately. The response includes Workshop IDs, titles, links, similarity measures, evidence, and actual index coverage, without the host's local file paths. The service does not need the Steam API key to answer scans; discovery and indexing on the host remain separate jobs.

A result covers only Workshop vehicle files that the host has downloaded **and indexed**. A finished public metadata crawl is not a finished content index, and the service cannot check private, deleted, inaccessible, or still-unindexed items. The similarity and confidence figures describe overlap with the indexed collection, not proof that a creation is original or copied.

## Fill your existing Steam Workshop folder

The `download-workshop` command searches public Stormworks Workshop metadata, reuses item folders already present in your Steam installation, and downloads accessible missing or outdated items into that **same** folder. It saves discovery cursors, refresh state, and download state in `workshop.sqlite`, so you can stop with Ctrl+C and rerun it. SteamCMD runs from a separate staging folder.

In **Command Prompt**, from this project folder:

```bat
py -3 -m stormcopy setup-steamcmd --cache steam-cache
steam-cache\steamcmd.exe +login anonymous +quit
py -3 -m stormcopy set-api-key
py -3 -m stormcopy --db workshop.sqlite download-workshop --workshop-folder "C:\Program Files (x86)\Steam\steamapps\workshop\content\573090" --cache steam-cache
```

`set-api-key` prompts for your personal Steam Web API key without putting it in the command or Command Prompt history. On Windows it saves the key in your user environment, so later Command Prompt sessions and the current one can use it without setting the variable again. A nonempty `STEAM_API_KEY` already set in the current Command Prompt takes precedence over the saved key. Run `py -3 -m stormcopy set-api-key --prompt` to replace the saved key or `py -3 -m stormcopy set-api-key --clear` to remove it. The saved key is available to programs running as your Windows user; keep your Windows account private. On non-Windows systems, put `STEAM_API_KEY` in your shell profile instead.

If discovery stops at HTTP 403, check the key shown on [Steam's Web API key page](https://steamcommunity.com/dev/apikey). Copy the key value once, then run `py -3 -m stormcopy set-api-key --prompt`. If you had also set `STEAM_API_KEY` in the current Command Prompt, open a new Command Prompt after replacing the saved key or run `set STEAM_API_KEY=` in that window before retrying. The downloader does not start SteamCMD processes until discovery succeeds.

The existing Workshop folder is detected automatically in the standard Steam location, but `--workshop-folder` lets you choose another Steam library. The folder must already exist and end in `steamapps\workshop\content\573090`. If anonymous SteamCMD access fails for an item, sign in to SteamCMD with an account that can access the content and add `--login ACCOUNT_NAME` to the last command.

By default this resumes the full public Workshop metadata crawl until its last page. Once that baseline is complete, later runs check recently updated items. Automatic downloads reuse a successful refresh for six hours; each new sweep starts seven days before the previous sweep's starting time, so changes near a run boundary remain in scope. Every 30 days, the next run starts another full published-order crawl to catch older or missed items. Both crawls resume after interruption. `--restart-discovery` starts a full crawl immediately. The report distinguishes **current pass complete** from **full public catalog crawl complete**; a short refresh does not erase the baseline coverage status. A database from an earlier version that already completed its full crawl runs one complete updated-order sweep before switching to the shorter overlapping refreshes.

After discovery, the command tries every accessible queued item in bounded groups of 100 by default. Known file sizes are sorted smallest first; Steam does not provide a global size-sorted Workshop query, and items without reported sizes come afterward. A full run can take a long time and a lot of storage. Use `--pages 10 --max-items 100` for a trial, then rerun without those limits to continue. The command keeps 20 GB free by default; adjust with `--reserve-free-gb 40`, for example. On Windows it starts up to four isolated SteamCMD processes by default. To try more during a bounded run, add `--processes 12`; the supported range is 1–16. `--workers` is an alias for the same setting. The downloader automatically splits the default ten-item batches into smaller ones when needed to give the requested processes work. A chunk with fewer queued items than requested processes uses only the available number. The progress display shows worker preparation, active processes, elapsed time, and an approximate ETA after enough items finish to measure the rate. The Workshop download ETA covers the whole selected queue and carries across groups; it can change when item sizes, Steam speeds, or genuine rate limits change. A full public discovery pass has no known page total, so that phase cannot show a reliable ETA. The final report shows discovery mode and coverage, existing folders, downloaded items, failures, and the remaining queue. Folder modification times are used as a freshness estimate for already installed items.

For example, after a trial run succeeds:

```bat
py -3 -m stormcopy --db workshop.sqlite download-workshop --workshop-folder "C:\Program Files (x86)\Steam\steamapps\workshop\content\573090" --cache steam-cache --processes 12 --max-items 100
```

More processes use extra disk space for separate SteamCMD installations, manifests, and downloads, and can increase disk activity. They may improve speed if Steam and your connection permit it, but are subject to Steam's access and rate limits. The downloader pauses new batches and reduces the active process count when Steam reports a rate limit.

To fill only IDs already in your database without an API key, use `download-workshop --known-only`. To defer fingerprinting until the files are present, add `--download-only`, then run:

```bat
py -3 -m stormcopy --db workshop.sqlite index-dir "C:\Program Files (x86)\Steam\steamapps\workshop\content\573090"
```

That indexing step also picks up existing Steam folders that were not yet in the search index. SteamCMD and the Steam API can only reach items your account may access; private, removed, or unavailable items will remain missing. A completed public metadata pass does not guarantee that the whole Workshop was downloaded. The downloader backs off on reported rate limits and gives failed items a retry delay.

At the prompt, paste the path to the XML file or drag the file into Command Prompt, then press Enter. You can also provide the path directly:

```powershell
py -3 -m stormcopy --db workshop.sqlite scan "C:\path\to\vehicle.xml"
```

The file is scanned against the index; it is not added as a Workshop reference. The Command Prompt report shows the overlap status, best matching item and Workshop link, directional structure similarity, MinHash estimate, heuristic confidence, and example matching positions in readable text. Use `--json` after a command if another program needs the structured result, for example `scan "C:\path\to\vehicle.xml" --json`. Progress stays on the error stream so JSON output remains parseable. Colours appear in a compatible interactive terminal; `--no-color` or the `NO_COLOR` environment variable turns them off. Nothing is uploaded to a remote server.

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

Without a Steam Web API key, the downloader refreshes missing or week-old tags in 50-item public batches, selects matching **known items**, then downloads the smallest reported files first. Private or removed IDs are retried after a day instead of on every run. `tags --force` rechecks every known item's tags immediately. With a key saved through `set-api-key` or supplied through `STEAM_API_KEY`, a filtered download discovers a matching Workshop page or reuses a refresh completed within six hours, then refreshes missing or week-old tags for known items before selecting downloads. Each exact filter has its own saved full-crawl and refresh state. Add `--discover-pages 10` to search ten pages first, `--discover-pages 0` to finish the current matching crawl or refresh pass, or `--known-only` to use only IDs already in the database. You can also run `discover --tag Vehicle --tag Air --exclude-tag Wip --pages 10` separately. Steam tag names should match the published tags exactly for Workshop search; `tags` shows names already seen in your index. Tag filtering does not guarantee an item contains a vehicle XML.

On Windows, `download` starts up to four SteamCMD processes by default. Each process has its own SteamCMD files and Workshop manifest under `C:\steamcmd\.stormcopy-workers`; completed items are moved into the usual `C:\steamcmd\steamapps\workshop\content\573090` folder. Worker setup uses extra disk space once, but downloaded item folders are moved rather than copied. SteamCMD must be installed directly in the selected cache folder for parallel mode. Use `--processes 1` for a SteamCMD executable elsewhere, or if your Steam account rejects concurrent sessions. `--processes` and its alias `--workers` accept 1–16. More processes may increase download speed when one SteamCMD session cannot use your connection; compare a bounded run with four, eight, and twelve if the network remains idle. Steam does not publish a guaranteed download rate or safe parallel-session limit, so actual speed depends on Steam, your account, disk, and connection. The downloader backs off and reduces active processes if Steam explicitly reports a rate limit.

SteamCMD normally starts once per batch of ten items, and the default delay is 0.5 seconds between batch starts. With more requested processes, the batch size is reduced automatically so a bounded chunk can occupy the requested processes. `--batch-size` accepts 1–50, and `--delay 0` removes the extra pause. The result reports elapsed time, requested and used processes, and effective batch size. Runs are resumable: successful items are not downloaded again unless their Workshop update time changes. Failed items get a retry cooldown so they do not block the rest of the queue. Only one downloader can use a cache at a time. `--max-items 0` processes every queued item. To redownload one item, use `--item-id 1234567890 --force`. Start with a bounded batch to check disk space and access. SteamCMD may require an account with access to Stormworks content; use `--login ACCOUNT_NAME` if anonymous access fails and sign in to SteamCMD interactively first. Run SteamCMD once on its own before the downloader, since its first launch may self-update and exit early. If a Steam installation already holds the items, `index-dir` can index those files without downloading them again.

To discover more public item IDs at scale, save a personal Steam Web API key once. From **Command Prompt**:

```bat
py -3 -m stormcopy set-api-key
py -3 -m stormcopy --db workshop.sqlite discover --pages 100
py -3 -m stormcopy --db workshop.sqlite refresh-sizes
py -3 -m stormcopy --db workshop.sqlite download --cache "C:\steamcmd" --max-items 100 --batch-size 10
```

`discover` saves the next cursor, so running it again resumes. It crawls the full published listing first, then uses an updated-order search with a seven-day overlap for later runs. An explicit `discover` command starts a new refresh as soon as the previous one finishes. A full published crawl repeats every 30 days. `--pages 0` finishes the current crawl or refresh pass; a full crawl can be long. `--restart` starts a new full published crawl. `refresh-sizes` fills missing reported sizes for known IDs without an API key. HTTP 429 and transient API errors back off and retry. SteamCMD failures are recorded in SQLite. Keep the API key private and protect any search service exposed outside localhost.

The default discovery command now handles maintenance automatically. For a manual updated-order search, `discover --sort updated --restart --pages 10` still uses its separate saved cursor. Steam offers no Workshop query sorted by size: the detector sorts only the IDs discovered so far, and the size is the primary published file's size rather than a guaranteed total installed folder size. Complete public-item coverage requires finishing the full published-order cursor pass and downloading every accessible vehicle item; private, removed, and inaccessible items remain outside the index. A complete download can require substantial time and storage.

## What a match means

The detector extracts Stormworks `<c>` components with `<vp>` grid positions. It sorts XML attributes and unordered child records, normalizes decimal spellings, ignores regenerated IDs and cosmetic paint/name fields, and keeps bodies separate so two bodies at the same local coordinate do not form a false structure. It hashes component orientation/content and each radius-two neighborhood. It also builds **spatial winnowing** fingerprints from contiguous component runs along each grid axis. These runs are ordered by coordinates, so rearranging XML elements does not break them. Logic links use relative endpoint positions; substantial microprocessor scripts and internal graphs add independent evidence.

Each file gets a 64-value **MinHash** sketch with 16 **LSH** buckets for close overall matches. A bounded index of rare neighborhood, winnowing, logic, and microprocessor hashes retrieves smaller copied sections that whole-vehicle LSH can miss. A scan merges those candidates, then loads compressed full fingerprints and compares exact multiset counts before ranking. New indexes store full fingerprints once per file in compressed form and keep only a bounded sample of hashes as lookup rows, limiting database growth. The saved index and download cache are reused across scans.

Workshop IDs are stored in SQLite's indexed primary key, allowing sorted pages and quick lookup. Each structurally matched result is checked against that catalog. IDs alone contain no vehicle structure, so comparisons search fingerprint indexes rather than sorting the full ID list on every scan. Candidate fingerprints are loaded and ranked one at a time to keep memory use bounded.

The reported **similarity percentage** is the share of the submitted vehicle's neighborhood instances found in the Workshop vehicle; `workshop_coverage_percent` measures the reverse. `combined_similarity_percent` is a heuristic blend of available geometry, winnowing, logic, and microprocessor coverage. The MinHash estimate is approximate whole-vehicle set Jaccard and can be low even when a real 20% section matches. Evidence includes matching neighborhood and sequence positions; a cluster of nearby rare matches can raise a partial-copy result to medium suspicion. A high label still requires broad rare structural overlap.

Some genuine Stormworks files use numeric XML attribute names (`00="1"`) and repeated attributes. The parser repairs those names in memory for fingerprinting; it never changes Workshop files on disk. Nonvehicle XML such as standalone microcontrollers is counted as ignored. Rerunning `index-dir` on an existing folder upgrades its vehicle files to the new search data and adds previously skipped vehicles.

Confidence is a **heuristic**, not a probability or finding of plagiarism. Generic shapes, common templates, and independently built geometry can match. The tool deliberately says “no strong match in indexed set” when nothing significant is found. The report includes indexed item/file counts, MinHash/LSH upgrade coverage, and whether the full public catalog crawl reached its end; it cannot establish originality against private, deleted, inaccessible, or unindexed items. Winnowing needs a shared contiguous component run to guarantee a selected hash; the neighborhood index provides a second route for shorter or interrupted assemblies.

## Steam access and coverage

Steam's [QueryFiles API](https://partner.steamgames.com/doc/webapi/IPublishedFileService) supports public Workshop metadata queries with cursor pagination (deeper than the 1,000-page `page` limit), but requires an API key. It is not a bulk vehicle-file endpoint. [Steam Workshop's item installation API](https://partner.steamgames.com/doc/api/isteamugc) and SteamCMD retrieve item content subject to account/game access. Stormworks Workshop content is stored in the Steam `steamapps/workshop/content/573090/<item id>` tree; addons may contain several `vehicle_*.xml` files. This project indexes every usable vehicle XML in each item. Some items may contain no vehicle XML or may not be downloadable by your account. Steam publishes no guarantee that one account can enumerate and download the entire Workshop, so full historical coverage must be measured rather than assumed. Previously discovered items remain in the local database if they later become private or are removed; a completed crawl means the listing was traversed, not that every retained item is still public.

## Validation

```powershell
py -3 -m unittest discover -s tests -v
```

The tests check XML order/metadata/translation tolerance, copied subassemblies, unrelated geometry, and unsafe XML rejection. Live Steam discovery/download requires your key and Steam access and is not part of the offline test suite.
