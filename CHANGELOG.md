# Changelog

All notable changes to Qobuz Librarian are recorded here, newest first. The project follows [semantic versioning](https://semver.org/); dates are when each version was tagged during local development.

## [0.13.0] - Unreleased

0.13.0 is mostly about interrupted work: keeping the app honest about what
finished, what did not, and what can safely happen next.

- Downloads keep their real progress and recovery choices through retries and restarts. History and post-job notifications now agree with the final result.
- Search keeps different editions and track versions apart, while Library review choices survive refreshes and restarts.
- Repair, Migration, Downsample, and Lyrics leave partial or unverified work recoverable instead of assuming it finished cleanly.
- First-run settings save, logs keep the configured amount of history, and crowded Queue and History controls wrap properly on desktop and phone. Authentication and offline states are clearer too.
- Startup catches unsafe overlap between music, staging, and backup paths. Docker passes through the web streaming settings, and release checks cover more of the image that is actually shipped.

## [0.12.1] - 2026-08-03

Fixes from driving 0.12.0 rather than reading it, led by a multi-disc download that imported perfectly and stopped the queue anyway.

- A multi-disc album no longer fails after a clean import. Its cover sits in the album root, which beets never searches, so the cover was left behind in staging - and a leftover reads as a download that never finished, which paused every download after it. The cover now goes where beets files it.
- A download that finished but left something behind now settles on the first Retry instead of the second, and says what it did rather than blaming a restart that never happened.
- A download blocked from the terminal no longer strands the whole app. It offered no way to settle it, told you to correct a path or permission problem that didn't exist, and survived the restart it recommended, while the web pointed at a Retry button that only exists for downloads started in the web app - so nothing could download or scan until a folder was deleted by hand on the server. The terminal now offers to clear it, and every message points at the surface that can.
- The dashboard says when downloads and scans are paused, and what to do about it. Only a second running copy of the app ever announced itself; the other seven reasons left the screen looking perfectly normal until you pressed something and were bounced.
- A button that's paused no longer blames terminal mode when terminal mode isn't the reason. It names the real one.
- A finished download stops being marked as failed because a different download's recovery is outstanding. It was reading one app-wide flag instead of its own, so one blocked item relabelled everything in History at once and only a restart put it back.
- A finished job keeps its log. It was held in memory only, so any restart replaced the record of what a download actually did with "No log output was retained for this job".
- A run that stopped and paused the queue no longer signs off with a green tick and "0 failed". A parked album downloads its tracks without one of them failing, so the summary judged the whole run on track counts and contradicted its own "0/1 albums OK" in the same breath; it now says how many albums were left unfinished.
- A download stopped for recovery points at the Retry button it actually has, instead of sending you to a Diagnostics page with no control for it.
- Multi-disc albums are recorded against the album folder, not the first disc folder. That mis-scoping flattened repaired refills into the album root, named the disc folder as the album in a single track's Undo record, and pushed a phantom artist into the Downsample review.
- A queued album the exact-recovery lane can't plan for - a small gap fill, an expanded-edition switch - now downloads instead of failing before a single track starts, and failing the same way at every launch after that.
- The album walk survives an album parking for recovery: it keeps the saved queue as it stands and says so, instead of ending in a traceback at the moment the app correctly paused.
- Ctrl-C during an artist scan stops the walk, which is what it already said it would do.
- Retrying a single-track download no longer inherits the first job's Undo record, which left two jobs offering to undo the same file.
- A repair whose refills all failed to download reports that, instead of reporting it as a file that couldn't be placed.
- A saved file the app couldn't read now says so on the dashboard and in Settings, with where the unreadable copy was kept. Until now only the container log mentioned it, so quality and downsample settings could quietly revert to defaults.
- Keyboard shortcuts run from one place. Ctrl+/ and Alt+/ are no longer swallowed into the search box, and the info notices on Dismissed pages are tinted instead of grey.
- Bulk downloads and dismissals notice a page that went stale and reload, instead of reporting work the server actually refused.
- A stray file sharing an artist folder's name no longer stops a scan, and a queued receipt says "1 album" rather than "1 albums".
- Library migration can't act on a directory number it already released if reserving one fails partway.
- On a phone, a stopped download's Retry button is no longer sliced off by the edge of its card - the row's chips, time and buttons wrap instead of being held on one line.
- History stops calling everything on the page "jobs" directly above a section named Jobs holding a different number.
- A Retry that won't settle can be diagnosed: the log now records what the attempt decided instead of nothing at all.
- A job that can't be written to History names the job in the log rather than blaming one particular field of it.
- Dependencies and CI actions updated.

## [0.12.0] - 2026-08-02

A truth-and-polish pass from a full audit of 0.11.3: summaries that report what actually happened, screens that keep your place, and a CLI that can be scripted.

- A corrupt state file is no longer silently replaced with an empty one. Every store (dismissed albums, upgrade caps, saved scans, settings, lyrics bookkeeping) now sets the unreadable copy aside as `….corrupt` and says what may have been reset, so one bad write can't erase weeks of curation with no trace. And the app stopped manufacturing that corruption in the first place: every store now writes through one crash-safe path - synced to disk before it replaces the old copy, with proper locking where the web app and CLI write the same file.
- Dismissing an album can no longer bury a different album that shares its name: the identity key kept "Alone" and "Alone (Again)" - or Rancid's two self-titled records - under one fingerprint, so dismissing one hid both. Distinct titles now stay distinct while remasters, deluxe editions and hi-res variants still fold onto their album; an existing dismissed list re-keys itself automatically.
- A lyrics run during a provider outage no longer records "no lyrics found" - a verdict that suppressed those tracks for 30 days. Tracks whose every query died on a connection failure stay retryable, and the summary now counts the run's own work ("7 checked · 12 skipped (already checked)") instead of claiming the whole library was scanned.
- The Library review keeps your place: refresh or Back reopens the artist you were inside and returns to the same scroll position. The sticky header and footer no longer slice album rows on every scroll - their offsets are measured from the real elements instead of guessed.
- The Dismissed pages paginate, filter, and gained "Bring all back", which returns everything to a live review in one tap; undoing a big dismissal no longer means confirming each artist separately against one enormous page.
- Dismissing speaks one language everywhere: Dismiss / Bring back, a Dismissed page, and a minus icon instead of a crossed-out eye (Downsample keeps its own "Keep hi-res" wording and a bookmark icon, because that action keeps).
- The offline screen matches the app - its own look, fonts, and your chosen theme - and Retry returns to the page you were on instead of Search.
- A bulk download's receipt names the album, links the queue, and surfaces the Background-work strip immediately, matching the single-download receipt.
- The upgrade walk no longer presents albums that left your library since the scan, calls them high-confidence, and finishes with a green tick; they're set aside and counted, and the summary owns up when nothing was upgraded.
- Repair jobs whose kept-originals folder is gone from disk stop pointing at an empty Diagnostics page; the job says what happened and offers Acknowledge so the alarm has an exit.
- The CLI grew flags for the three menu-only modes (`--library-walk`, `--album-gaps`, `--repair`), honours `--yes` on an unattended downsample walk instead of declining every artist and reporting success, exits nonzero when a walk fails or is cut short (matching its documented exit codes), and lays its menus and messages out for the terminal actually in front of you - a phone screen included.
- The CLI also rejects mode combinations it would silently half-run (`--library-walk --migrate` used to start a migration and drop the walk), and the artist-mode modifiers (`--include-singles`, `--include-comps`, `--no-catalog`) work with `--library-walk`, which honours them.
- The web UI and CLI report the same version from one source; the web UI could previously report a neighbouring project's version from a stray pyproject.toml.
- Release safety: every push and pull request now proves the Docker image still builds (previously nothing did until a release was already public) and runs the destructive-operation checks - the suite that proves downsample, repair and the gap-fill backup can't eat files, which nothing ran automatically before. Dependabot no longer proposes single-line edits to the generated image lock, which only regenerates as a whole.

## [0.11.3] - 2026-07-22

A migration fix for filesystems that don't track file access time.

- Library migration works again on ZFS datasets with `atime=off`, and on any filesystem that doesn't report an access time. The check that proves a source or destination folder is genuinely itself was demanding the access-time field even though it never reads it, so it refused to migrate with "safety could not be proved". The same proof backs the backup step behind Upgrade, Downsample, and Repair, so those are covered on these filesystems too. Downloads were never affected.

## [0.11.2] - 2026-07-15

A first-run fix and a documentation accuracy pass.

- A music folder that Docker created as root no longer traps a first download behind a restart: the quick start creates the folder up front, and the writability check probes live. Fixing ownership on the host takes effect immediately and always agrees with what Settings → Diagnostics shows.
- Docs now match current behaviour: a verified repair retires its recovery backup (only an unprovable one is kept), and the folder-ownership guidance lives in the permissions section.

## [0.11.1] - 2026-07-15

Fixes from driving every flow end to end against a live library: downloads, cancels, crashes, restarts, repairs.

- Cancelling a download now discards the partial work and moves on; it used to leave a blocked queue item that paused every download until the container restarted.
- A download that came back incomplete, or died in a crash, can be retried straight from its job page; before, the retry refused and only a restart or manual file surgery cleared it.
- Parallel downloads no longer risk a clean album being quarantined because its tracks finished out of order, and a gap-fill that only re-downloaded cover art imports instead of reporting a false failure.
- Backups survive restarts and permission fixes: the container no longer touches every file's ownership on boot, and saved backup records tolerate ownership or permission changes instead of refusing with "this backup changed".
- Failed upgrades put the original album back again on Docker setups: the automatic restore compared its copy against the wrong file list and always gave up, leaving the album displaced into the backups folder.
- A leftover download folder from a crashed terminal-mode session no longer freezes the whole app at startup; it's set aside for review and the app comes up normally.
- Repair now diagnoses and fixes files the app user doesn't own (wrong PUID, NAS permissions) instead of silently skipping them, a verified repair is reported as repaired, and the originals' backup is cleaned up once every replacement is verified in place.
- Settings → Diagnostics says why a backup was kept and gains a Remove button that deletes one only after checking, byte for byte, that everything it holds is already back in the library.
- "Check new releases" moved next to the Library page's refresh icon, so it stays reachable while a review is parked.
- Signing in uses new cookie names; you'll be asked to sign in once after upgrading.
- Smaller fixes: an empty bookkeeping folder no longer keeps the staging-leftovers warning permanently lit.

## [0.11.0] - 2026-07-14

A long reliability pass over everything that moves or deletes files.

- Interrupted downloads, imports, migrations, backups, and Undo now recover by re-checking what's actually on disk; anything that can't be verified is left in place instead of guessed at.
- Undo on a single track removes exactly the file the job recorded, plus any folders it created that are now empty: replaced files, reused folders, and symlinks survive.
- When the data folder can't enforce the single-writer lock, library writes stay paused instead of running unprotected.
- beets 2.12.0 is now required and verified before imports run; it's bundled in the Docker image, so this only affects bare CLI installs, which must provide that exact version. Cleanup stays inside the staging folders the import opened.
- Albums credited to several artists can be re-filed under the primary artist again: the move now survives crashes and never overwrites an existing folder.
- Smaller fixes from just after 0.10.3: review dismissal and mobile disclosure rendering, scans pick up changed candidate settings, cleaner web/terminal handoff coordination, and container user ids are normalised before the privilege drop.

## [0.10.3] - 2026-07-10

Reliability fixes across the app: mostly review-lifecycle fixes, plus clearer waiting states.

- Downloading everything in the Library review no longer resurrects the same albums as "missing" on the next visit: a fully worked-through review retires, and anything that failed to download comes back ticked and ready to retry instead of vanishing until the next scan.
- Failed downloads from a partial approve also fold back into the open review, ticked, rather than surviving only as an error line in a finished job.
- The Library review survives restarts and discarded jobs: it rebuilds from the last scan's saved state, so the page can't end up saying the baseline is ready while showing nothing. A worked-through or discarded review now gets a proper finished page with the tabs still visible, a link to dismissed albums, and a "Bring all back" action.
- New-release batches can no longer leak their albums into the Library review's fold-back paths: their results live and die with their own job.
- The first downsample's keep-or-delete-originals answer now takes effect for the run that asked it. It previously could sit deferred behind the running job, and a "keep" choice risked being applied too late.
- The downsample cap now follows the album itself (by release identity, not folder path), so moving or renaming a downsampled album no longer re-opens it as an upgrade candidate, which would have re-downloaded hi-res you deliberately shed.
- Retrying a parked download review after fixing your Qobuz token in Settings now works without restarting the container; the retry no longer holds onto the dead token it was parked with.
- Undo on a single-track download no longer hangs while a long job (a lyrics scan, a migration) is using the library; it tells you what's busy and to try again after.
- A download waiting behind a library-wide lyrics scan or a migration now says what it's waiting for instead of sitting on "Running" with no explanation.
- The terminal's `--check-new-releases` now applies the same guards as the web check: the catalogue-limit re-baseline, the singles-suppression setting, and the baseline merge behave identically in both.
- Small fixes: the History tab heading, scan progress copy, and the dismissed-count on tab-scoped reviews read correctly; `AUTO_LIBRARY_SCAN` documentation now describes both things it controls.

## [0.10.2] - 2026-07-05

Reliability and interface polish over 0.10.1.

- Reorganising a downloaded album into its primary-artist folder now verifies every file at the destination before deleting the original: a library split across two drives can't be left with the album half-moved.
- Search shows placeholder rows while it fetches results, instead of a lone spinner on an empty panel.
- The library and tool reviews are a cleaner hairline list: one row per artist instead of a boxed panel each, nicer down a long list and on a phone.

## [0.10.1] - 2026-07-04

Fixes from running 0.10.0 against a full-size library, including album-matching and downsample hardening.

- Approving a review confirms first, with the download count in the prompt.
- Review checkboxes respond instantly on large libraries.
- The first scan reports each phase instead of looking hung.
- Empty folders no longer count as owned albums (library review and Search), so a genuinely-missing album can't silently vanish from the review.
- Downsampling no longer clips loud masters: hot files are eased just below full scale first; quieter albums are untouched.
- First downsample asks once whether to keep a backup of your hi-res originals or delete them to save space; the choice saves to Settings.
- Parked reviews are out of the Queue: you open each from its own tab (Library, Downsample, Upgrade, Repair) or from History; the Queue badge counts only running and waiting work.
- Confirmations are drawn in-app; logins survive restarts and image updates.
- Big reviews: dismiss a row without expanding it, load more as you scroll, instant ticks.
- Phone fixes: tab-bar icons, scroll clearance, queue badge, tappable cards, no reconnect-notice flash.
- Wording and layout cleanups across Upgrade, Downsample, Queue, History, Settings, and Repair.

## [0.10.0] - 2026-07-02

This one is mostly about the interface. The old UI worked but never felt like part of the app, so 0.10.0 replaces it end to end: a proper night theme, a light theme derived from the same palette instead of a stock one, a real layout on phones (bottom tab bar, no hamburger), and a new mark. Search moved to the front page since it's what you open the app to do.

Under it, scanning got simpler. There used to be separate scans for missing albums, gap fill, upgrades, and downsample candidates; "Scan library" now builds all four from one pass over your library, resumes if interrupted, and tells you when it couldn't check every artist rather than pretending it finished clean. The Library page owns the whole flow now, scan through review.

Downloads also verify themselves before import: the staged FLACs are checked against what your quality tier should have produced, with one automatic retry from the highest source when Qobuz under-delivers. A retry that comes back with fewer tracks than the first attempt is thrown away instead of trusted. Anything still short gets flagged in History.

The per-artist Artist page is gone; artist browsing lives in Search and whole-library work lives on each tool's page. If you had a parked per-artist review from an older version it will show as failed with a note to re-run the scan.

There's also a batch of quality-of-life work that came out of daily use: the Library page shows what's actually on disk (track counts and space by quality tier, and what a downsample pass would reclaim), downsampling can keep the hi-res originals for a week so it can be undone from Settings, stranded backups get a Restore button instead of a terminal command, new-release checks run on a real timer even when nobody opens the app, and the notification hook can push to ntfy or Discord out of the box and also tells you if your Qobuz token stops working.

Smaller fixes: review checkboxes no longer lose rapid clicks when the same review is open in two tabs, Settings only persists the fields you actually change (so `.env` edits keep applying), the job log matches the app theme, and icons/theme-color follow the active theme properly. beets is at 2.12, htmx at 2.0.4, and the image now runs on Python 3.14.

Upgrading: pull the new image and restart; existing logins, tokens, and library state carry over. Tested against a 0.9.4 data volume directly.

## [0.9.4] - 2026-06-25

**Search and review**

- Search results are grouped by album and show what is already in your library. A matched album shows "In library" with no download button; other editions (remaster, deluxe, a live take) are grouped under expandable "other versions" you can still download. Quality upgrades stay on the Upgrade page, keeping search focused on finding music. Albums filed under a collaboration folder are now matched correctly.
- Closing a scan review now returns to the queue with the scan parked and reopenable; discarding is a separate, confirmed action. Select-all, clear-all, and "dismiss unselected" are available from the review screen, with dismissed albums recoverable from the Dismissed albums page.
- Dismissing a review's last album completes it instead of leaving the job stuck on an empty list with an old "0 new releases" banner.
- "New releases" means new to the saved Qobuz catalogue baseline and within the release-age window, so a back-filled old album no longer shows as new (window `NEW_RELEASE_MAX_AGE_DAYS`, default 365 days).

**Library and tools**

- The Library page now treats the full scan as a one-time baseline. After the baseline exists, new-release checks become the main action, while a full re-scan remains available when needed.
- The queue and history spell out destructive actions: bulk actions show how many they affect ("Cancel all N jobs", "Clear all N finished jobs") and each per-job control says what it does (remove from queue, stop, or discard the scan).
- Cancelling a queued job takes effect at once: it leaves the queue the moment you cancel it, instead of sitting as "Queued" until the job ahead of it finishes.
- The Library and maintenance tool pages use consistent naming and warnings; Downsample and Lyrics run without a Qobuz token; an unconnected account gets a setup prompt rather than a warning; and Settings holds back the operational toggles until a token is saved.

**Polish and fixes**

- The search box is one joined bar at every width, the dashboard leads with search and recent activity, a single-track release reads "1 track" rather than "1 tracks", and a failed download says what actually went wrong rather than a catch-all.
- The navigation menu closes when you tap outside it, not only when you tap the button again, and the dismissed-albums list moved out of the menu (it stays one tap away on the Library page and after any review).
- Approving a review with nothing selected used to flip the job to done over an empty set; it now keeps the job in review and says nothing was selected.
- Saved Qobuz credentials are flushed to disk durably, the dismissed-album store is safe against two processes writing it at once, and the lyrics pass no longer prints raw status codes and counter dicts to the log.
- Mobile polish: dismissed-album artist names no longer truncate to "R…", history lines wrap instead of clipping, and small-screen headers stack cleanly.

**Setup and docs**

- compose.yaml forwards the documented `.env` knobs it previously dropped, and a new `WEB_BIND` sets the host interface the UI binds to. The README, configuration docs, and example env are brought in line with the current UI.

## [0.9.3] - 2026-06-23

**Data-safety polish**

- In-place migration to a destination short on space is blocked before files move, matching the CLI safeguard. The Migrate screen includes an explicit low-space override. Copy mode still warns rather than blocks, because the source library is left intact.
- The migration space preview now counts the cover art, booklets, and `.cue`/`.log` sidecars that get carried alongside the audio, so the estimate matches what the copy actually writes; previously a library with large booklets could see an estimate that was too low, and an in-place move could read "0 bytes" while still copying art.
- When a parked album finally imports on a retry, any non-audio companions it left behind (booklets, scans, cover art) are now preserved outside the staging folder before cleanup, the same protection the upgrade path already had.

**Correctness**

- An artist's discography no longer stops paginating early if Qobuz returns a page with a few malformed entries mixed in, which could silently hide some of that artist's albums during a scan.
- Fuzzy-match thresholds set via the environment are clamped to their valid 0–1 range, so a typo like `CONSOLIDATE_THRESH=-1` can't quietly turn duplicate cleanup into "match everything."
- The gap-fill "will downsample to…" note now respects your download-quality tier: at CD-lossless it no longer promises a downsample that won't happen.
- Saved Qobuz credentials are now flushed to disk durably, matching the web-login credential write, so a crash right after saving can't roll back a token the UI reported as saved.
- The hidden/single-album store is now safe against two processes writing it at once (a web dismissal during a CLI hand-off), via a cross-process lock and unique temp files.

**Setup, docs, and release polish**

- `compose.yaml` now forwards the documented `.env` knobs that it previously dropped: `WEB_AUTH_PASSWORD_FILE`, the free-space floor, the repair cache/pacing settings, beets path/plugin overrides, and the live-album filter. Setting them in `.env` now actually takes effect.
- New `WEB_BIND` controls the host interface the UI is published on; set `WEB_BIND=127.0.0.1` to keep it off the LAN. `WEB_HOST` remains the in-container bind for non-Docker runs.
- New releases are described everywhere as listed for review rather than "pre-ticked": the review screen leaves them un-ticked so they cannot all be queued by accident.
- Configuration docs now state the real new-release and catalogue-cache defaults, clarify that Settings covers the common behaviour knobs while advanced ones stay in `.env`/Compose, and fix the migration-results filename, lock-handoff, and CLI-container wording. The Docker image's licence metadata now reflects the third-party (GPL) tools it bundles, and the release smoke test verifies the compiled stylesheet is actually served.

## [0.9.2] - 2026-06-23

**New-release check needs a baseline first**

- New-release checks now require the baseline produced by a full library scan. Until that baseline exists, "Check for new releases" is disabled with an explanatory note, direct requests are refused, and the automatic daily check waits behind an interrupted baseline scan.

## [0.9.1] - 2026-06-23

**Reviews no longer duplicate**

- Re-running a scan (repair, library gap-fill, upgrade, or downsample) replaces its earlier pending review instead of stacking duplicate review cards on the dashboard.

**New-release checks use the saved baseline**

- The new-release check flags albums in an artist's catalogue that are not in the baseline, including older albums newly added to Qobuz. Candidates default to un-ticked so a review cannot queue a large set of downloads in one tap.

**Repair scan: cleaner live activity**

- A whole-library repair scan now shows its progress as a single status line under the progress bar: `Scanning "<artist>" · N albums · M flagged`, refreshing a couple of times a second, instead of appending a "still scanning…" line to the activity log every few seconds. The activity log now lists only flagged albums (the actual findings), and a finished scan no longer keeps hundreds of heartbeat lines.

**Repair runs on one page**

- A repair scan now stays on the Repair page from start to finish: scanning, reviewing the flagged albums, and the repair itself all happen there and update live, instead of handing you off to a separate job page partway through. A parked review is no longer reachable only behind a "Start scan" button that would have discarded it.

**Clearer job status**

- A queued job now shows what it is waiting behind instead of only "Queued"; multi-album jobs keep progress on the full run rather than resetting per album; and the Upgrade, Downsample, and Lyrics pages show when they last ran.

**Safety fixes**

- Undo on a single-track download can no longer remove a same-numbered track from a different disc of a multi-disc album. A flood of failed logins can no longer lock the admin out (a request that already carries a valid session skips the limit), and an unreadable credentials file now fails closed instead of re-opening first-run setup. A library migration to a destination short on free space no longer starts an unattended run that would relocate files until it ran out.
- Upgrading an album now carries its booklets, scans, `.cue`/`.log`, and hand-placed cover art into the rebuilt folder instead of discarding them, matching the single-album path. Consolidation moves overlapping tracks to a recoverable backup rather than deleting them, repair no longer removes an album folder it failed to recognise, and a near-full disk stops the download queue cleanly for a retry rather than failing each album in turn.

## [0.9.0] - 2026-06-21

The repair scan was rebuilt for broader detection, faster scans, and clearer live progress. This release also includes reliability and safety fixes. The only changed default is removal of the unusable 320 kbps tier.

**Repair catches truncated files that still play**

- The whole-library repair scan now checks every track's length against its exact Qobuz recording, not only visibly short files. A track cut short at a frame boundary, with its FLAC header rewritten to the shorter length, decodes cleanly and passes the size check, so the old sweep marked it intact and moved on, and a genuinely damaged album could scan green. Every ISRC-tagged track is now duration-verified (the command-line sweep too).

**Faster, and re-scans skip the network**

- The sweep now checks several artists at once instead of one at a time, making the first scan of a large library several times quicker. Per-track Qobuz lookups are cached: a re-scan, or any album that shares a track's ISRC, skips the network round trip. Files are still decode-tested fresh on every scan, so new corruption is still detected. Set `REPAIR_CACHE_ENABLED=false` to skip the lookup cache, or `REPAIR_CACHE_TTL_DAYS` to change how often a cached lookup re-verifies against Qobuz.

**Clearer repair progress**

- A clean library can produce long periods with no findings. The scan now shows the current album, a periodic "still scanning…" heartbeat with a running album count, and an elapsed clock, with the activity log open by default.

**Fresh downloads are double-checked**

- After an album finishes downloading, its track lengths are re-checked against Qobuz. The downloader already discards tracks that won't decode, but a clean truncation (decodes fine, header rewritten short) could slip past that; now it's caught right after the download with a note to repair it, instead of waiting to be found by a later scan.

**Backups verify contents, not just size**

- Cross-filesystem backup, restore, and gap-fill now verify the copy by hashing its contents before the original is deleted, instead of trusting a matching file count and total byte size. A same-size corruption (a transfer glitch, or a partial write re-padded back to length) used to pass the size check, and the source was then removed, leaving the damaged copy as the only one. The copy is now compared byte-for-byte and any mismatch aborts the operation with the original left untouched.

**Download quality**

- The 320 kbps MP3 tier is removed. The pipeline is FLAC-only and the post-download cleanup discards any non-FLAC file, so choosing that tier downloaded each track and then deleted it: the setting silently fetched nothing. It's gone from Settings and the docs, and an existing `STREAMRIP_QUALITY=1` is now coerced to CD lossless (the smallest lossless tier) with a clear message rather than passed straight through.

**Container runs as the user you asked for**

- A non-numeric `PUID`/`PGID` (a typo) used to log a warning and then silently run the container as root, defeating the non-root isolation. It now refuses to start; running as root requires the explicit, valid pair `PUID=0 PGID=0`.

**Review selection matches the server**

- Select-all and the per-artist select now tick boxes only after the server confirms each save. A failed save used to leave boxes ticked while the server held none, so approval acted on a selection you never really made; a failure now flags the affected boxes and leaves the rest alone so you can retry.

## [0.8.0] - 2026-06-20

Quality-of-life and reliability improvements across search, scanning, and the web UI.

**Search & scanning**

- Search returns more results, so big artists surface properly.
- Whole-library scans now show the full set instead of capping the list, and prolific artists are no longer cut short.
- Artists sort by name ignoring a leading "The"/"A"/"An" (so "The Beatles" files under B).

**Web UI**

- The Search page lays out correctly on narrow phone screens.
- Improved web UI responsiveness under load and fixed list/pagination edge cases.

**Under the hood**

- A range of correctness and reliability fixes across downloads and library maintenance, plus tighter build checks.

## [0.7.0] - 2026-06-18

Strengthens the library repair scan so it can no longer report a corrupt file as intact, plus two smaller correctness fixes. No changed defaults.

**Repair scan**

- The whole-library repair scan now decode-tests every FLAC instead of trusting its size and STREAMINFO header. A file with frame-CRC damage or a zeroed-out middle keeps its original size and reported duration, so the old size-and-header check passed it as "verified intact" and the scan reported no damage. Every file is now run through `flac -t` locally (no network): a clean file still costs no Qobuz call, a file that won't decode is surfaced and refilled, and when the `flac` tool is missing a file is counted "unverified" rather than silently "ok". The scan summary now reports what was actually decode-verified.

**Offline page**

- The offline page's Retry button works again. It loaded a small script that was never shipped in the image, so the button did nothing; it is now a plain link that still works while the service worker is serving the page.

**Dismissed-album list**

- A corrupt hidden-albums file is now moved aside to a `.corrupt` copy with a warning instead of being silently overwritten by the next dismissal. Previously one unreadable read returned an empty list and the next hide or restore wrote a fresh file over it, destroying a dismissed-album list curated over weeks with no trace.

## [0.6.1] - 2026-06-13

Bugfix release: seventeen edge-case fixes, no new features or changed defaults.

**Backup safety**

- The age sweep now proves each track in an upgrade backup is actually back at its origin path (same relative filename, at least as many bytes) before reaping the backup. File-count matching was fooled when a gap-fill or other operation added a different file to the origin while one of the backup's own tracks was still missing there. Previously that could silently destroy the only surviving copy of the unreturned track.
- An upgrade backup kept because the re-rip couldn't be verified as complete (e.g. a truncated-but-decodable track shrank the playtime) now gets an explicit keep-marker. A same-count, larger hi-res re-rip could look redundant by bytes alone and be reaped on the next sweep; the marker stops that.
- The beets import override now always forces `move: yes`. A user beets config with `copy: yes` was silently leaving every newly-downloaded album in staging, which the pipeline's success check read as "import failed" and parked.
- Retrying parked albums now checks whether the audio actually left disk before removing the parking entry. A beets run that exits 0 while skipping a library duplicate (under `duplicate_action: skip`) used to trigger cleanup on the strength of the exit code alone, deleting the only copy.

**Single-track download and undo**

- Downloading the last missing track of an album now clears the "downloaded single" mark an earlier partial download may have left. Without this the album's artist stayed hidden from bulk scans and the new-release check even after you completed the album.
- The upgrade walk now keys the "skip downloaded singles" check on the Qobuz artist name, not the folder name. A folder called "Beatles" where Qobuz says "The Beatles" was leaking the downloaded single back into upgrade candidates.
- The single-track undo now takes the cross-process run lock before deleting any files or touching the beets database.
- The undo track-match now uses the `tracknumber` field (the one `read_album_dir` actually writes). Also, two tracks with no ISRC and no track number on record can no longer accidentally match each other and delete the wrong file.

**Consolidation and repair**

- Consolidation stops immediately under `--dry-run`: it deletes overlapping tracks, so letting it run was a dry-run violation.
- Repair stops under `--dry-run` before moving any files aside, for the same reason: repair moves the truncated originals out of the way before re-ripping, so an interrupt could have stranded them.
- A sibling FLAC whose quality cannot be read (broken STREAMINFO or no title tag) now shows as "quality unreadable" and requires the same explicit DELETE confirmation as a clearly better-quality track. Previously it was silently counted as safe to delete.

**Web and CLI polish**

- The settings page keeps the token you just typed in the (masked) field when Qobuz rejects it, so you can fix a paste slip without re-entering the whole thing.
- Pasting an album URL into Tracks mode now says the link is an album URL and to switch to Albums, instead of a silent empty result.
- An interrupted repair scan now tells you to start the repair scan again (which resumes from the checkpoint), not the library scan.

## [0.6.0] - 2026-06-09

- **Single-track downloads.** Search has a Tracks mode with a *Get track* button that pulls one track into the right `Artist/Album (Year)/` folder, never a full-album rip. It's recorded as a deliberate single, so scans and the new-release check don't treat that artist as one you're collecting; finish the album later and it files as a normal complete album. **Undo** removes the track and any empty folder it created, and the Upgrade walk leaves single-track downloads alone unless you set `UPGRADE_SINGLES_ENABLED`.
- Two quick retries of the same album can no longer double-queue it; retry now re-checks for a job already touching that album under the submit lock.
- The downsample step caps the ffmpeg encode at ten minutes, so a track on a hung NFS or FUSE mount fails with a clear message and leaves the original untouched instead of pinning a worker forever.
- Behind a reverse proxy the entrypoint passes `--proxy-headers` and honours `FORWARDED_ALLOW_IPS`, so the login rate-limiter sees each client's real address instead of the proxy's and stops locking everyone out at once.

## [0.5.0] - 2026-06-05

First packaged release during local development. Major additions included:

- **Migrate** mode turns an existing or partially tagged collection into the `Artist/Album (Year)/` layout the rest of the tool expects. It reads each file's tags first and can fall back to AcoustID fingerprinting; copy mode leaves the originals in place, and anything it cannot place confidently is left alone and listed in a manifest.
- ISRC-anchored **repair** now snapshots a truncated file's tags before it goes and restores them onto the refilled track, and backs up the source by ISRC before replacing it: a crash mid-refill can no longer strand a track.
- The awaiting-review list pages by artist and keeps its selection on the server, so approving thousands of candidates no longer rides on form state.
- Lyric state and the retry manifest are locked across processes; rejected staging files are quarantined instead of silently left in place.

## [0.4.1] - 2026-05-27

- A corrupt fetch-log line can no longer 500 the dashboard.
- `Retry-After: 0` from Qobuz is honoured instead of being treated as no header.
- An unrecognised `STREAMRIP_QUALITY` warns loudly rather than defaulting to the most permissive cap.

## [0.4.0] - 2026-05-21

- **Check for new releases**, across the whole library or one artist, compares each artist's current Qobuz catalogue against the saved baseline and surfaces albums newly added to that baseline, flagged for review. It reads the catalogue listing alone, so it's about one API call per artist.
- On-disk caches (album fetches, parsed FLAC tags keyed on path+mtime+size, and artist catalogues with a TTL) turn a re-scan of an unchanged library into seconds instead of minutes.
- Jobs survive a container restart: an awaiting-review list comes back, and an interrupted job returns marked as such with a retry hint instead of vanishing.

## [0.3.1] - 2026-04-30

- Multi-disc folders detect disc numbers for non-FLAC tracks.
- Two upgrade-backup restore edges (equal-byte and empty-backup-dir) no longer block automatic recovery.

## [0.3.0] - 2026-04-28

- **Upgrade** mode re-rips albums Qobuz can now serve at a higher quality, backing up the originals first.
- **Downsample** mode shrinks hi-res FLACs above 44.1/48 kHz, each verified to decode cleanly before it replaces the original.
- **Repair** finds truncated or short FLACs and refills exact tracks by ISRC when matching is safe, leaving good files untouched.
- **Lyrics** mode backfills lyrics across tracks already on disk.

## [0.2.1] - 2026-04-03

- The dashboard's stale-token banner flips the moment the API rejects the token, instead of only checking at startup.
- Cancelling a queued download stops cleanly instead of leaving a half-finished album to be swept into a later import.

## [0.2.0] - 2026-03-26

- A web UI (FastAPI) for searching, downloading and watching jobs stream their log live, alongside the existing CLI.
- A crash-safe persistent download queue that resumes after a restart, with a shared-data run lock so the web app and CLI in one stack cannot write at the same time.
- Whole-library and per-artist gap scans that list every missing album.
- Ships as a multi-stage Docker image with a compose stack.

## [0.1.0] - 2026-01-29

- First working version: download a single Qobuz album or a whole artist, scan a local library to know what's already there, and import cleanly with beets so only the genuinely missing tracks are fetched.
