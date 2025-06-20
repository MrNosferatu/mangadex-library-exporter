# MangaDex Library Exporter

A command-line tool to export your MangaDex manga library to various formats and optionally sync unlisted manga to AniList.

## Features
- Export your MangaDex library to:
  - MyAnimeList (MAL) XML format
  - CSV format
  - JSON format
- Identify manga without MAL or AniList IDs
- Optionally add manga (with AniList ID) directly to your AniList account via the AniList API
- Handles AniList API authentication (Authorization Code Grant)
- Rate-limits AniList API requests to avoid bans

## Requirements
- Python 3.7+
- `requests` library

## Setup
1. Clone or download this repository.
2. Install dependencies:
   ```bash
   pip install requests
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
4. When exporting as MAL XML, it will export all manga without MAL ID into a seperate file and some of those manga might have AniList ID. If you choose to add those manga to your AniList Account, follow the instructions to authenticate and authorize the app.

## AniList API Authentication
- You will be guided through the Authorization Code Grant flow.
- You need to create an AniList API client at [AniList Developer Settings](https://anilist.co/settings/developer).
- Set the redirect URI to `https://anilist.co/api/v2/oauth/pin` or your own callback URL.

## Output Files
- `export/manga_library.xml`: MAL XML export
- `export/manga_library.csv`: CSV export
- `export/manga_library.json`: JSON export
- `export/unlisted_by_MAL.csv`: Manga without MAL ID but might still have  AniList ID
- `export/unlisted_by_MAL_&_AL.csv`: Manga without MAL ID and AniList ID

## Notes
- Session is only valid for the duration of the script run (not saved to disk).
- AniList API requests are rate-limited to 30 requests per minute.
- Technically you can add all manga with AniList ID to your AniList account, but its more time efficient to import the MAL XML format via onsite importer because of the limited rate for AniList API.

## License
MIT
