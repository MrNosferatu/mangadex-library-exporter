# MangaDex Library Exporter

A command-line tool to export your MangaDex manga library to various formats and optionally sync your manga to AniList to limited extent.

## Features
- Export your MangaDex library to:
  - MyAnimeList (MAL) XML format
  - CSV format
  - JSON format
- Identify manga linked to MAL or AniList
- Optionally add manga that linked to AniList directly to your AniList account via the AniList API
- Handles AniList API authentication (Authorization Code Grant)
- Rate-limits AniList API requests to avoid bans

## Requirements
- Python 3.7+
- Install dependencies from `requirements.txt`:

  ```bash
  pip install -r requirements.txt
  ```

## Setup
1. Clone or download this repository.
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

## Usage
1. Run the script:
   ```bash
   python main.py
   ```
2. Log in with your MangaDex credentials when prompted.
3. Choose one or more export options (comma-separated, e.g., `1,3`):
   - `1`: Export MAL XML
   - `2`: Export JSON
   - `3`: Export CSV
   - `4`: Logout
   - `q`: Quit
4. When exporting as MAL XML, it will export all manga unlinked to MAL but might be linked to AniList into a seperate file. If you choose to add those manga to your AniList Account, follow the instructions to authenticate and authorize the app.

## AniList API Authentication
- You will be guided through the Authorization Code Grant flow.
- You need to create an AniList API client at [AniList Developer Settings](https://anilist.co/settings/developer).
- Set the redirect URI to `https://anilist.co/api/v2/oauth/pin` or your own callback URL.

## Output Files
- `export/manga_library.xml`: MAL XML export
- `export/manga_library.csv`: CSV export
- `export/manga_library.json`: JSON export
- `export/unlinked_to_AniList.xml`: Manga that are unlinked to AniList
- `export/unlinked_to_MAL.csv`: Manga that are unlinked to MAL but might linked to AniList
- `export/unlinked.csv`: Manga that are unlinked to MAL and AniList

## Notes
- Session is only valid for the duration of the script run (not saved to disk).
- AniList API requests are rate-limited to 30 requests per minute.
- Technically you can add all manga with AniList ID to your AniList account, but its more time efficient to import the MAL XML format via onsite importer because of the limited rate for AniList API. With 30 requests per minute, it can take a long time to add all manga, that's why the script only adds manga that are unlinked to MAL but might be linked to AniList.

## License
MIT
