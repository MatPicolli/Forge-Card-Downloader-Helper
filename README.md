# ⚔️ Forge Card Downloader

Downloads MTG card images from [Scryfall](https://scryfall.com) and organizes them for the [Forge](https://github.com/Card-Forge/forge) game client.

Lightweight pure-Tkinter GUI — only two pip dependencies (`requests` and `scrython`).

## Features

- **Full set downloads** — Browse, filter and select sets from Scryfall's database. Download all card images in parallel.
- **Card search (all prints)** — Fuzzy search by name, shows every printing across all sets. Download individual versions or all at once.
- **Missing card scanner** — Scans your Forge card folder and compares against Scryfall to find which images are missing, then downloads them.
- **Threaded downloads** — Adjustable thread count (1–16) for parallel image fetching.
- **Retry with backoff** — Automatic retry with exponential backoff on connection errors and HTTP 429 (Too Many Requests).
- **Forge set code mapping** — Parses Forge's `res/editions/*.txt` files to correctly map between Scryfall codes and Forge folder names (e.g., Scryfall `sth` → Forge folder `SH`). Falls back to a built-in dictionary of ~60 known mappings.
- **Progress bar** — Fixed-width progress bar with card name shown above it (no layout jumps).

## Requirements

- Python 3.10+
- Tkinter (included with most Python installations)

## Installation

```bash
pip install -r requirements.txt
python forge_card_downloader.py
```

## Configuration

In the **Settings** tab:

1. **Forge image folder** — Usually:
   ```
   C:\Users\YourUser\AppData\Local\Forge\Cache\pics\cards
   ```

2. **Forge editions folder** — Usually:
   ```
   C:\Users\YourUser\AppData\Roaming\Forge\res\editions
   ```
   or
   ```
   InstalationFolder\Forge\res\editions
   ```
   If found, the app parses these files for accurate set code mappings.

3. **Download threads** — 1 to 16 concurrent image downloads.

4. **Image quality** — `small`, `normal`, `large` (default), `png`, or `border_crop`.

## How set code mapping works

Forge sometimes uses different codes than Scryfall for image folders:

| Set | Scryfall | Forge folder | Reason |
|-----|----------|-------------|--------|
| Unlimited | `2ed` | `U` | `Code2` field in edition file |
| Stronghold | `sth` | `SH` | `Code2` field in edition file |
| Kaladesh | `kld` | `KLD` | No discrepancy |

Resolution order:
1. Parsed from `res/editions/*.txt` if available
2. Built-in dictionary (~60 known mappings)
3. Default: Scryfall code uppercased

## Image file format

Images are saved in Forge's expected format:
```
{SetFolder}/{Card Name}.fullborder.jpg
```

## License

Personal use. Respect [Scryfall's API terms](https://scryfall.com/docs/api).
Card images are property of Wizards of the Coast.
