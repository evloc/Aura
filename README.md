# AURA

A music player and a workbench for your collection, in one Windows app. The
**Listen** side plays your music, with playlists, favorites and a queue. The
**Organize** side (formerly Unreleased) fixes tags, groups versions and clears
out duplicate copies.

Everything runs on your own machine. Nothing is uploaded anywhere. It edits your
real files in place, and it never deletes anything.

---

First launch asks for your music folder. Point it at one and hit
**Start scanning**.

**SmartScreen.** The first run may show "Windows protected your PC" because the
file isn't code-signed. Click **More info → Run anyway**. Anyone you send it to
will see the same thing, so warn them or it looks broken.

---

## Listen

The switch at the top flips between **Listen** and **Organize**. Both sides
share one player bar at the bottom.

**Songs.** Your whole library, sorted by title, artist or recently added. Click
a song to play it — the rest of the list becomes the queue. In title or artist
order, drag down the **A–Z bar** on the right edge to jump through the list.
Songs you grouped in **Versions** show as one row with an **N versions** button
to pick a take (turn this off in Settings → Player).

**Artists and Albums.** Grids of each, with search. Albums are grouped by the
Album tag (and Album artist when it's set); songs with no Album tag are left out.

**Playlists.** Make your own, add songs from any song's **⋯** menu (or
right-click), or drag songs onto them. Reorder by dragging or with the arrows;
rename or delete from **⋯**. Smart playlists build themselves: **Favorites**,
**Most played**, **Recently played**, **Recently added** and **Top rated**
(4–5 stars from Organize). Any of them can be saved as a normal playlist.

**Favorites.** Tap the heart on a song, in the player bar or on Now Playing.

**Drag and drop.** Drag any song — in Listen or in the Organize list (it takes
every ticked track) — and a panel appears on the right: drop on **Play next**,
**Add to queue**, **Favorites**, a playlist or **New playlist…**. You can also
drop onto a playlist card, or onto the open Up next panel.

**Queue.** **⋯ → Play next** puts a song right after the current one; **Add to
queue** puts it at the end. **Up next** (player bar) shows what's coming: drag
or use the arrows to reorder, remove songs, or clear the rest. Shuffle reorders
what's coming up (turning it off puts the order back), and Repeat cycles off →
all → this song.

**Now Playing.** Click the artwork in the player bar (or press **N**) for the
big view: blurred-artwork background, large controls, a **Versions** row to jump
between takes of the same song, and a side panel with **Lyrics** and **Up next**.

**Lyrics.** Shown on Now Playing. **Add lyrics / Edit** there, or **Lyrics…**
in the Organize editor, writes them into the song file itself (MP3, M4A, FLAC,
OGG), so other players see them too.

**Synced lyrics (they move with the song).** When lyrics have timings, Now
Playing lights up the current line, fades the ones you've passed and scrolls by
itself, like Apple Music. Click any line to jump there. Scroll to read ahead —
it waits a few seconds before following the song again. Timings come from:
- **Auto-sync** — AURA listens to the song with a speech model that runs on
  your PC (Whisper, nothing is uploaded) and times each line of the lyrics you
  already have. Works on unreleased songs and your own tracks. When it's done
  you get a review window: lines it couldn't hear clearly are marked **check**
  and their times are a guess — click a time and re-tap, or nudge with `−`/`+`.
- **Transcribe** — for a song with no lyrics anywhere, AURA writes the words
  down itself, already timed. Expect to fix some words, especially on
  autotuned or mumbled parts.
- **Sync by hand** — play the song and press `Space` right as each highlighted
  line starts (`Backspace` undoes a tap, `−`/`+` nudge a line by 0.1 s, click
  a time to re-tap from there). One listen per song.
- **Find lyrics** — a search window that checks **LRCLIB** (often already
  timed) and **Genius** (fans post lyrics for a lot of leaks and unreleased
  songs). The search box is filled in for you with leak tags like `[v2]`,
  `(prod. …)`, `snippet` or `CDQ` stripped out; edit it to try other names.
  Click a result to preview it, then **Use these** or **Use + auto-sync**.
- **`.lrc` files** — `Song.lrc` next to `Song.mp3` is picked up automatically,
  and so are timed lyrics already inside a file.
Timed lyrics are saved into the file (MP3: the standard synced-lyrics frame
next to the plain lyrics; other formats: LRC text in the lyrics tag).

**Lyrics auto-sync settings.** Settings → *Lyrics auto-sync*. Pick the model:
**Fast** (145 MB), **Better** (480 MB, the default) or **Best**
(1.6 GB, slower). It downloads once, the first time you auto-sync, into the
`models` folder next to your library. **Auto-sync every song that has lyrics
but no timings** works through your whole library in the background: songs it's
confident about are saved straight away, the rest are listed with a **Check**
link. With **Better**, expect around a minute per song on a typical PC.

**Lyrics search.** Type a line you remember into the Songs search; songs whose
lyrics contain it show up with the matching line.

**Between songs.** Settings → Player → *Between songs*: **No gap** (the next
song is loaded ahead of time so it starts instantly) or a **crossfade** of
2–12 seconds.

**Resume.** Close AURA and it reopens on the same song, at the same spot, with
the same queue, shuffle, repeat and volume — paused, ready to go.

**New music shows up by itself.** AURA watches your music folders and rescans
when files are added, changed or removed, then tells you how many new songs it
found. Turn it off in Settings → Player if you prefer pressing Rescan.

**Mini player.** The small-window button in the player bar (or **M**) opens a
compact always-on-top player with artwork, play/pause, skip and the heart.
Needs a recent Edge or Chrome.

**Sleep timer.** The moon button stops the music after 15–90 minutes (with a
short fade) or at the end of the current song.

**Light theme.** Settings → Player → Theme: Dark, Light or Match Windows.

**Volume leveling.** AURA measures each song's loudness in the background
(Settings → Sound shows progress) and evens them out, so quiet demos don't
make you reach for the volume after a loud master. A limiter stops boosted
songs from clipping. On by default.

**Equalizer.** Settings → Sound: Bass boost, Treble boost, Vocal, Car, Loud &
clear, Lo-fi, or drag the five sliders for your own.

**Playback speed.** The `1×` button on Now Playing: 0.75× to 1.5×, pitch kept.

**Stats.** Listening time today, this week, the last 30 days and all time;
a 30-day chart and what time of day you listen; top songs and artists; and a
button to shuffle the songs you've never played.

**Discord status.** Settings → Discord status shows "Listening to *song* by
*artist*" with a progress bar on your Discord profile while music plays, and
clears it when you pause. One-time setup (two minutes): at
discord.com/developers/applications click **New Application**, name it
**AURA**, copy its **Application ID** into Settings, and tick the box.
Optionally upload a picture named `aura` under **Rich Presence → Art Assets**
— it's shown when a song has no cover, and as a small badge otherwise.

**Covers on Discord.** Discord can only show a picture from a web link, not
from a file on your PC, so:
- **Released songs** — AURA finds the official cover in Apple's iTunes catalog
  by title and artist (on by default; no account, nothing uploaded). Version
  labels are ignored, so *Wishing Well (V2)* still gets the Wishing Well cover.
- **Your own / unreleased songs** — optional and **off by default**: AURA can
  upload the cover from the file to catbox.moe, a free image host, once per
  cover. Anyone with that link can see the image, so only turn this on if
  you're fine with that artwork being public.
The status shows straight away with the AURA logo and switches to the cover
a moment later. Links are remembered, so nothing is looked up or uploaded
twice; a song that wasn't found online is checked again after a week.

**Play counts.** A song counts as played once you've heard half of it (or 30
seconds). Counts feed Most played and Recently played.

**Edit tags** from any song's **⋯** menu jumps to that song in Organize.

Playlists, favorites and play counts live in the app's library, not inside the
MP3s. A song whose file disappears drops out of them and comes back if the file
returns. Media keys and the Windows media controls work too.

---

## Sharing AURA and updating it

AURA can be installed from a normal setup file and updates itself from your
GitHub releases. The one-time setup:

1. **Make a public GitHub repo** (for example `AURA`) and put this folder in it
   — `.github`, `installer.iss` and `version.json` included. It has to be
   public so your friends' copies can download updates without a password.
   (`.gitignore` keeps your own library and build files out of it.)
2. **Release a version:** `git tag v1.0.0` then `git push origin v1.0.0`.
   GitHub builds `AURA.exe` and `AURA-Setup.exe` on a Windows machine (about
   10 minutes — watch the **Actions** tab) and publishes them under
   **Releases**.
3. **Send your friend `AURA-Setup.exe`** from that release page. Installing
   needs no admin rights; it adds AURA to the Start menu (and the desktop if
   they tick it) and can be removed from Windows' *Installed apps* list.

**Every update after that:** change the code, commit, push, then tag a higher
version (`git tag v1.1.0`, `git push origin v1.1.0`). When anyone opens AURA it
checks GitHub, shows **"AURA 1.1.0 is available — Update"**, and clicking it
downloads the installer, installs it silently, and reopens AURA — nobody
re-downloads anything by hand. Settings → About & updates also has **Check for
updates**. Libraries, playlists, favorites and settings live in
`%APPDATA%\Unreleased\` and survive every update and even an uninstall.

Versions are compared as numbers (1.10.0 is newer than 1.9.0). Releases need
a `v` + number tag. A test build without publishing: Actions → *Release AURA*
→ *Run workflow*, then download it from the run's Artifacts.

**SmartScreen.** The installer isn't code-signed (signing certificates cost
money), so the first time someone runs `AURA-Setup.exe` Windows shows "Windows
protected your PC" → **More info → Run anyway**. Updates AURA downloads itself
don't show this.

A copy built on your own PC with `build.bat` works fully but doesn't update
itself — only release builds know where to look. `build.bat` also makes
`AURA-Setup.exe` if Inno Setup 6 is installed.

---

## Filling in tags

The **Tags** tab is the fast way to go from a folder of badly-named files to a
properly tagged library.

**Applies to every track.** Set artist, album artist, album, genre and year
once. Anything you leave blank is skipped.

**Read titles from filenames.** Works out a clean title from each filename:

| Filename | Title | Producer |
|---|---|---|
| `01 - headlights (feat. Someone) [prod. Producer One] 320.mp3` | Headlights (feat. Someone) | Producer One |
| `03. dnd (prod by Producer Two).mp3` | Dnd | Producer Two |
| `redline_CDQ_v2.mp3` | Redline V2 | |
| `Midnight Drive - Copy.mp3` | Midnight Drive | |
| `07_track_name_ft_Nobody_prod_Someone.mp3` | Track Name (feat. Nobody) | Someone |

It strips track numbers, `(1)` copy markers, `- Copy`, and bitrate/quality junk
(`320`, `128kbps`, `CDQ`, `HQ`), then lifts out `feat.` and `prod.` credits. It
is deliberately conservative — it never invents anything that isn't in the name,
it leaves deliberate styling alone (an ALLCAPS or all-lowercase title keeps its
look only if the name is genuinely mixed case), and it won't strip a leading
number that's part of the title, so `21 Guns` and `99 Problems` survive.

By default it only fills fields that are **empty**. Tick *Overwrite* to replace
what's already there.

Nothing is written until you've seen the preview. Untick any row you disagree
with, then **Write tags**.

---

## Convert to MP3

Settings → **Convert to MP3** shows how many songs aren't MP3 yet (WAV, FLAC,
M4A, OGG, OPUS, WMA, AIFF) and converts them all; or in **Library** search
`notmp3`, tick the songs you want and press **Convert to MP3…**. Pick the
quality (320 kbps is the default) and what happens to the originals:
- **Move into an `_originals` folder** (default) — the folder sits in your
  music folder, AURA never scans it, and nothing is deleted.
- **Leave them where they are** — you keep both copies.
Title, artist, album, cover art, rating and lyrics (timed ones too) are copied
into the MP3, and favorites, playlists, play counts and versions move to it.

## Undo

Every bulk write is snapshotted before it happens — auto-fill, bulk edits, and
single-track saves. **Undo** in the top bar reverses the last one, and the Tags
tab keeps the last 20 with an Undo button each. Undo doesn't just fix the
database; it writes the old values back into the files.

`Ctrl`+`Z` does the same thing.

Renaming files is the one exception — that changes names on disk and is not
covered by undo, so it asks first.

---

## Library

Click a track, edit it on the right. Real ID3 tags: title, artist, album artist,
album, year, genre, track number, BPM, key, producer, comment, cover art. Plus
a 1–5 star rating, free-text notes, and your own comma-separated tags — those
three are stored in the file as custom `TXXX` frames, so they travel with it to
another drive. Other players ignore them.

Tick the checkboxes for bulk work: set the artist, rate a batch, drop one cover
image onto many tracks at once, or rename files from the pattern in Settings.

**Search** does more than plain text:

| | |
|---|---|
| `prod:someone` | producer contains |
| `song:redline` | tracks grouped under that song (see Versions) |
| `artist:` `title:` `album:` `genre:` `tag:` `note:` `folder:` | field contains |
| `bpm:>140` `rating:>=4` `bitrate:<192` | numeric comparison |
| `noart` `untagged` `unrated` `versioned` `unversioned` | flags |
| `-word` | exclude |
| `"exact phrase"` | quoted phrase |

Terms combine, so `prod:someone rating:>=4 -live` works.

---

## Versions

For songs you have more than one take of — demo, mixdown, final, instrumental,
sped up. The **Versions** tab groups them as one song so you can keep every
one of them and still tell them apart on your phone.

**Suggested groups.** Files that differ only by a version word in the name or
tags are found automatically: `Redline`, `redline_v2`, `Redline (Instrumental)`,
`Redline final` become one suggested song, with a label worked out for each
(`V2`, `Instrumental`, `Final`). Version words are only trusted at the end of a
name or inside brackets, so a title like *Clean Slate* is left alone. A group
needs two genuinely different recordings to be suggested — exact copies of one
file are a job for Duplicates.

For each suggestion: edit the song name or any label, then **Group as versions**,
or **Not versions** if it's wrong (it won't be suggested again). The ✕ on a row
says that one file isn't part of the song.

**By hand.** Tick tracks in the Library and use **Group as versions…**. Using a
song name that already exists adds them to it.

**Write titles.** Sets each file's title to `Song (Label)` — `Redline`,
`Redline (V2)`, `Redline (Instrumental)` — so every version is distinct in a
player that only shows titles. Several unlabelled takes become `Version 1`,
`Version 2`, oldest first, and a repeated label is numbered (`Demo`, `Demo 2`).
Tick *Also set Album to the song name* to keep versions together in an album
view. It's previewed first and covered by Undo.

**Protected from Duplicates.** Grouped versions are skipped by the two fuzzy
duplicate checks, so "Keep best, move rest" never offers to move one out of the
way. Byte-identical copies are still caught, and when one of a pair of copies is
in a group, that one is the pre-selected keeper.

Groups and labels live in the app's own library (`%APPDATA%\Unreleased\`), not
inside the MP3s. The titles you write do travel with the files.

---

## Duplicates

Four checks, ranked by confidence. Tracks you've grouped as versions are skipped by checks 3 and 4:

| | Check | Confidence |
|---|---|---|
| 1 | **Identical file** — byte-for-byte SHA-256 match | certain |
| 2 | **Same audio, different tags** — hashes only the audio, so a retagged copy still matches its twin | certain |
| 3 | **Sounds like the same song** — an acoustic fingerprint that survives re-encoding, so a 320k bounce and a 128k export of one track match | likely |
| 4 | **Similar name and length** — strips `final`, `v2`, `mixdown`, `(1)`, dates, then compares, requiring durations within 3 seconds | possible |

Each group pre-selects a keeper (best bitrate, then longest, then shortest
name). Change it, hit Play to check, then **Keep best, move rest** — or
**Keep all best, move all rest** to clear every group at once.

A file can be the keeper of one group and a loser in another. Anything chosen as
a keeper anywhere is protected and shows **KEPT ELSEWHERE**, so a bulk resolve
can never move every copy of a song.

**Nothing is ever deleted.** Losers are moved to a holding folder that the
scanner skips, so they never reappear as duplicates. Empty it yourself when
you're happy.

---

## Shortcuts

Press `?` in the app for the full list.

`Space` play / pause · `Ctrl`+`→` / `←` next / previous · `Shift`+`→` / `←`
skip 10 seconds · `N` Now Playing · `L` lyrics · `Q` Up next · `M` mini player ·
`Space` (in the sync window) time the next line ·
`F` favorite · `/` search · `Esc` close a panel · `Ctrl`+`S` save tags ·
`Ctrl`+`Z` undo · `↑` `↓` move through the Organize list

---

## Notes

- A FLAC or OGG file used to stop a scan partway through (and lock the
  library). Fixed: every format scans, and one unreadable file is skipped
  instead of stopping the scan.
- Tag editing is full for **MP3**. FLAC, WAV, OGG and M4A scan and play, and
  basic tags save, but ratings/notes/your-tags are MP3-only.
- Fingerprinting reads mp3, flac, wav and ogg with no extra install. M4A needs
  ffmpeg on your PATH; without it those files skip check 3.
- Scanning runs several files at once, so a few hundred tracks go quickly.
  Rescans skip anything whose size and date haven't changed.
- Library and settings live in `%APPDATA%\Unreleased\`. The exe can sit
  anywhere — nothing is written next to it.
- The app serves itself on 127.0.0.1 only. Every action that changes a file
  needs a token minted at startup and given only to its own window, so a random
  web page you have open can't drive it.
- Closing the window shuts the app down within about a minute. The ✕ quits
  immediately. Relaunching while it's running reopens the window instead of
  starting a second copy.
