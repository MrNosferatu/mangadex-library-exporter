import requests
import os
import json
import time
import webbrowser
from urllib.parse import urlencode, urlparse, parse_qs
import xml.etree.ElementTree as ET
import getpass
import csv
import xml.dom.minidom as minidom
from requests.exceptions import RequestException, ConnectionError, Timeout, HTTPError
from rich.progress import Progress, BarColumn, TextColumn, TimeElapsedColumn, TimeRemainingColumn, SpinnerColumn, TaskProgressColumn, MofNCompleteColumn
import concurrent.futures

MANGADEX_API = "https://api.mangadex.org"
LOGIN_ENDPOINT = f"{MANGADEX_API}/auth/login"
MANGA_LIBRARY_ENDPOINT = f"{MANGADEX_API}/manga/status"

# --- Session Management ---
SESSION_TOKENS = None
SESSION_CREDENTIALS = None  # Store (username, password) after first login

def login(username: str, password: str) -> dict:
    """Login to MangaDex and return the session tokens, or raise for invalid credentials."""
    resp = request_with_retry('POST', LOGIN_ENDPOINT, json={"username": username, "password": password})
    if resp.status_code == 401:
        # Try to extract error message from response
        try:
            error_data = resp.json()
            if error_data.get('result') == 'error':
                detail = error_data.get('errors', [{}])[0].get('detail', 'Invalid credentials')
                raise ValueError(f"Login failed: {detail}")
        except Exception:
            raise ValueError("Login failed: Invalid credentials (401)")
    resp.raise_for_status()
    return resp.json()


def save_session(tokens, credentials=None):
    global SESSION_TOKENS, SESSION_CREDENTIALS
    SESSION_TOKENS = tokens
    if credentials:
        SESSION_CREDENTIALS = credentials

def load_session():
    global SESSION_TOKENS
    return SESSION_TOKENS

def load_credentials():
    global SESSION_CREDENTIALS
    return SESSION_CREDENTIALS

def refresh_session(session, tokens):
    # Optionally implement token refresh if needed
    # For now, just return the same session
    return session, tokens


def ensure_valid_session():
    """Ensure a valid MangaDex session, prompt for login if needed."""
    tokens = load_session()
    session = None
    while True:
        if tokens:
            session = requests.Session()
            session.headers.update({"Authorization": f"Bearer {tokens['token']['session']}"})
        else:
            username = input("MangaDex Username: ")
            password = getpass.getpass("MangaDex Password: ")
            try:
                tokens = login(username, password)
                save_session(tokens, (username, password))
            except ValueError as ve:
                print(ve)
                continue
            except Exception as e:
                print(f"Unexpected error: {e}")
                continue
            continue
        try:
            # Try a lightweight request to check session
            get_manga_library(session)
            return session, tokens
        except requests.HTTPError as e:
            if e.response.status_code == 401:
                print("Session expired. Please login again.")
                tokens = None
                continue
            else:
                raise

# --- MangaDex API ---
def get_manga_library(session: requests.Session) -> dict:
    """Fetch the manga library (id and status) for the logged-in user."""
    resp = request_with_retry('GET', MANGA_LIBRARY_ENDPOINT, headers=session.headers)
    resp.raise_for_status()
    data = resp.json()
    if data.get("result") != "ok":
        raise Exception("Failed to fetch manga library")
    return data["statuses"]


def get_manga_info(session: requests.Session, manga_ids: list, status_map: dict = None) -> list:
    """Fetch manga info for a list of manga IDs (max 100 per request), and append reading_status if status_map is provided."""
    all_info = []
    # Fetch user language preferences
    try:
        resp = request_with_retry('GET', f'{MANGADEX_API}/settings', headers=session.headers)
        resp.raise_for_status()
        user_preferences = resp.json()
        user_language = user_preferences.get('settings', {}).get('userPreferences', {}).get('filteredLanguages', [])
    except Exception as e:
        print(f"Failed to fetch user preferences: {e}")
        user_language = ['en']  # Default to English if fetching fails
    batch_size = 100
    batches = [manga_ids[i:i+batch_size] for i in range(0, len(manga_ids), batch_size)]
    total_manga = len(manga_ids)
    with Progress(
        SpinnerColumn(),
        TextColumn("{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        transient=True,
    ) as progress:
        manga_task = progress.add_task("Processing manga...", total=total_manga)
        processed = 0
        def fetch_chapter_list(manga_id, user_language, read_chapter_ids, session):
            params = {
                'translatedLanguage[]': user_language,
                'limit': 300,
                'includes[]': ['scanlation_group', 'user'],
                'order[volume]': 'desc',
                'order[chapter]': 'desc',
                'offset': 0,
                'contentRating[]': ['safe', 'suggestive', 'erotica', 'pornographic'],
                'includeUnavailable': 1
            }
            try:
                resp = request_with_retry('GET', f'{MANGADEX_API}/manga/{manga_id}/feed', params=params, headers=session.headers)
                chapter_data = resp.json()
                chapters = [c for c in chapter_data['data'] if c['type'] == 'chapter' and c['id'] in read_chapter_ids]
            except Exception as e:
                print(f"Failed to fetch chapters for manga {manga_id}: {e}")
                chapters = []
            time.sleep(1)  # Cooldown for rate limit
            return chapters
        for batch in batches:
            params = {
                "ids[]": batch,
                "limit": 100,
                "contentRating[]": ["safe", "suggestive", "erotica", "pornographic"],
                "includes[]": ["cover_art", "artist", "author"]
            }
            resp = request_with_retry('GET', f"{MANGADEX_API}/manga", params=params, headers=session.headers)
            resp.raise_for_status()
            data = resp.json().get("data", [])
            # Batch fetch read chapter status for all manga in batch
            read_status_map = {}
            try:
                resp_read = request_with_retry('GET', f'{MANGADEX_API}/manga/read', params={'ids[]': batch, 'grouped': 'true'}, headers=session.headers)
                read_data = resp_read.json().get('data', {})
                if isinstance(read_data, list):
                    read_status_map = {}
                else:
                    read_status_map = read_data
            except Exception as e:
                print(f"Failed to batch fetch read chapters: {e}")
                read_status_map = {}
            # Batch fetch ratings for all manga in batch
            ratings_map = {}
            try:
                rating_params = [("manga[]", mid) for mid in batch]
                resp_rating = request_with_retry('GET', f'{MANGADEX_API}/rating', params=rating_params, headers=session.headers)
                ratings_data = resp_rating.json().get('ratings', {})
                ratings_map = ratings_data if isinstance(ratings_data, dict) else {}
            except Exception as e:
                print(f"Failed to batch fetch ratings: {e}")
                ratings_map = {}
            futures = []
            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                for manga in data:
                    manga_id = manga.get("id")
                    if manga_id and manga_id in status_map:
                        manga["reading_status"] = status_map[manga_id]
                    read_chapter_ids = set(read_status_map.get(manga_id, []))
                    chapters = []
                    skip_chapter_fetch = False
                    if not read_chapter_ids:
                        chapters = []
                        manga['read_chapter'] = "0"
                        manga['read_volume'] = "0"
                        skip_chapter_fetch = True
                    if not skip_chapter_fetch:
                        futures.append((manga, executor.submit(fetch_chapter_list, manga_id, user_language, read_chapter_ids, session)))
                    else:
                        # No chapters to fetch, update progress
                        progress.update(manga_task, advance=1)
                        processed += 1
                for manga, future in futures:
                    chapters = future.result()
                    def parse_chapter_num(ch):
                        try:
                            return float(ch['attributes'].get('chapter') or 0)
                        except Exception:
                            return 0
                    def parse_volume_num(ch):
                        try:
                            v = ch['attributes'].get('volume')
                            return float(v) if v is not None else 0
                        except Exception:
                            return 0
                    if chapters:
                        max_chapter = max(chapters, key=parse_chapter_num)
                        max_volume = max(chapters, key=parse_volume_num)
                        manga['read_chapter'] = str(max(parse_chapter_num(max_chapter), 0))
                        manga['read_volume'] = str(max(parse_volume_num(max_volume), 0))
                    else:
                        manga['read_chapter'] = "0"
                        manga['read_volume'] = "0"
                    # Use batch ratings
                    manga_id = manga.get("id")
                    rating_info = ratings_map.get(manga_id)
                    if rating_info and 'rating' in rating_info:
                        manga['user_rating'] = rating_info['rating']
                    else:
                        manga['user_rating'] = 0
                    progress.update(manga_task, advance=1)
                    processed += 1
            all_info.extend(data)
    return all_info

def fetch_and_prepare_manga_info(session):
    """Fetch library and manga info from MangaDex."""
    manga_library = get_manga_library(session)
    manga_info = get_manga_info(session, list(manga_library.keys()), status_map=manga_library)
    return manga_info

# --- Export Functions ---
def export_unlinked_to_json(unlinked, filename='export/unlinked_by_MAL.json'):
    # Ensure the output directory exists
    os.makedirs(os.path.dirname(filename), exist_ok=True)
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(unlinked, f, ensure_ascii=False, indent=2)
    print(f"Exported {len(unlinked)} manga to {filename} (no MAL id)")


def export_unlinked_to_csv(unlinked, filename='export/unlinked_by_MAL.csv'):
    columns = [
        "MAL Id", "AL Id", "Type", "Title", "Description", "Original Language", "Demographic", "Status", "Year", "Content Rating", "Tags", "Author", "Artist", "Reading Status"
    ]
    reading_status_map = {
        "reading": "Reading",
        "completed": "Completed",
        "on_hold": "On-Hold",
        "dropped": "Dropped",
        "plan_to_read": "Plan to Read",
        "re_reading": "Reading"
    }
    os.makedirs(os.path.dirname(filename), exist_ok=True)
    with open(filename, "w", encoding="utf-8", newline='') as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(columns)
        for manga in unlinked:
            attributes = manga.get('attributes', {})
            links = attributes.get('links', {})
            mal_id = links.get('mal', '-') if isinstance(links, dict) else '-'
            al_id = links.get('al', '-') if isinstance(links, dict) else '-'
            manga_type = manga.get('type', '').capitalize() if manga.get('type') else ''
            title_dict = attributes.get('title', {})
            title = title_dict.get('en') or next(iter(title_dict.values()), '')
            description = attributes.get('description', {}).get('en', '')
            orig_lang = iso6391_to_language(attributes.get('originalLanguage', ''))
            demographic = attributes.get('publicationDemographic', '')
            demographic = demographic.capitalize() if demographic else ''
            status = attributes.get('status', '')
            status = status.capitalize() if status else ''
            year = attributes.get('year', '')
            content_rating = attributes.get('contentRating', '')
            content_rating = content_rating.capitalize() if content_rating else ''
            tags = ', '.join([t['attributes']['name'].get('en', '') for t in manga.get('attributes', {}).get('tags', []) if 'attributes' in t and 'name' in t['attributes']])
            author = ''
            artist = ''
            for rel in manga.get('relationships', []):
                if rel.get('type') == 'author':
                    author = rel.get('attributes', {}).get('name', author)
                if rel.get('type') == 'artist':
                    artist = rel.get('attributes', {}).get('name', artist)
            reading_status_raw = manga.get('reading_status', attributes.get('reading_status', ''))
            reading_status = reading_status_map.get(reading_status_raw, reading_status_raw.capitalize() if reading_status_raw else '')
            writer.writerow([
                mal_id, al_id, manga_type, title, description, orig_lang, demographic, status, year, content_rating, tags, author, artist, reading_status
            ])
    print(f"Exported {len(unlinked)} manga to {filename}")


def anilist_authorization_code_flow():
    print("AniList Authorization Code Grant Flow\n")
    client_id = input("Enter your AniList client ID: ").strip()
    client_secret = input("Enter your AniList client secret: ").strip()
    redirect_uri = input("Enter your AniList redirect URI (must match your app settings): ").strip()

    # Step 1: Direct user to authorization URL
    params = {
        'client_id': client_id,
        'redirect_uri': redirect_uri,
        'response_type': 'code'
    }
    auth_url = f"https://anilist.co/api/v2/oauth/authorize?{urlencode(params)}"
    print(f"\nPlease open the following URL in your browser and authorize the application:\n{auth_url}\n")
    try:
        webbrowser.open(auth_url)
    except Exception:
        pass

    # Step 2: User pastes the code from the redirect URL
    code = input("After authorizing, paste the 'code' parameter from the redirect URL here: ").strip()

    # Step 3: Exchange code for access token
    token_url = "https://anilist.co/api/v2/oauth/token"
    data = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "code": code
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    response = request_with_retry('POST', token_url, json=data, headers=headers)
    if response.status_code == 200:
        token = response.json().get("access_token")
        return token
    else:
        print(f"Failed to get access token: {response.text}")
        return None
    

def export_manga_list_to_xml(manga_info_list, filename="mangalist.xml", session=None):
    user_id = ""
    user_name = ""
    total = 0
    total_reading = 0
    total_completed = 0
    total_onhold = 0
    total_dropped = 0
    total_plantoread = 0
    exported = 0
    not_found = 0
    unlinked = []
    root = ET.Element("myanimelist")
    myinfo = ET.SubElement(root, "myinfo")
    ET.SubElement(myinfo, "user_id").text = user_id
    ET.SubElement(myinfo, "user_name").text = user_name
    ET.SubElement(myinfo, "user_export_type").text = "2"
    manga_status_map = {
        "reading": "Reading",
        "completed": "Completed",
        "on_hold": "On-Hold",
        "dropped": "Dropped",
        "plan_to_read": "Plan to Read",
        "re_reading": "Reading"
    }

    for manga in manga_info_list:
        mal_id = None
        attributes = manga.get('attributes', {})
        links = attributes.get('links')
        if isinstance(links, dict):
            mal_id = links.get('mal')
        if not mal_id:
            not_found += 1
            unlinked.append(manga)
            continue
        exported += 1
        # Use reading_status from manga if present, else default
        reading_status = manga.get("reading_status")
        if not reading_status:
            # Try to get from attributes if user manually merged
            reading_status = attributes.get("reading_status", "plan_to_read")
        status = manga_status_map.get(reading_status, "Reading")
        if status == "Reading":
            total_reading += 1
        elif status == "Completed":
            total_completed += 1
        elif status == "On-Hold":
            total_onhold += 1
        elif status == "Dropped":
            total_dropped += 1
        elif status == "Plan to Read":
            total_plantoread += 1
        manga_elem = ET.SubElement(root, "manga")
        ET.SubElement(manga_elem, "manga_mangadb_id").text = str(mal_id)
        title_dict = attributes.get('title', {})
        title = title_dict.get('en') or next(iter(title_dict.values()), '')
        ET.SubElement(manga_elem, "manga_title").text = title
        ET.SubElement(manga_elem, "manga_volumes").text = "0"
        ET.SubElement(manga_elem, "manga_chapters").text = "0"
        ET.SubElement(manga_elem, "my_id").text = "0"
        ET.SubElement(manga_elem, "my_read_volumes").text = manga.get('read_volume', "0")
        ET.SubElement(manga_elem, "my_read_chapters").text = manga.get('read_chapter', "0")
        ET.SubElement(manga_elem, "my_start_date").text = "0000-00-00"
        ET.SubElement(manga_elem, "my_finish_date").text = "0000-00-00"
        ET.SubElement(manga_elem, "my_scanalation_group").text = ""
        ET.SubElement(manga_elem, "my_score").text = str(manga.get('user_rating', "0"))     
        ET.SubElement(manga_elem, "my_storage").text = " "
        ET.SubElement(manga_elem, "my_retail_volumes").text = "0"
        ET.SubElement(manga_elem, "my_status").text = status
        ET.SubElement(manga_elem, "my_comments").text = " "
        ET.SubElement(manga_elem, "my_times_read").text = "0"
        ET.SubElement(manga_elem, "my_tags").text = ""
        ET.SubElement(manga_elem, "my_priority").text = "Low"
        ET.SubElement(manga_elem, "my_reread_value").text = ""
        ET.SubElement(manga_elem, "my_rereading").text = "NO"
        ET.SubElement(manga_elem, "my_discuss").text = "YES"
        ET.SubElement(manga_elem, "my_sns").text = "default"
        ET.SubElement(manga_elem, "update_on_import").text = "1"
    ET.SubElement(myinfo, "user_total_manga").text = str(exported)
    ET.SubElement(myinfo, "user_total_reading").text = str(total_reading)
    ET.SubElement(myinfo, "user_total_completed").text = str(total_completed)
    ET.SubElement(myinfo, "user_total_onhold").text = str(total_onhold)
    ET.SubElement(myinfo, "user_total_dropped").text = str(total_dropped)
    ET.SubElement(myinfo, "user_total_plantoread").text = str(total_plantoread)
    tree = ET.ElementTree(root)
    os.makedirs(os.path.dirname(filename), exist_ok=True)
    # Write to a string first
    import io
    xml_bytes = io.BytesIO()
    tree.write(xml_bytes, encoding="utf-8", xml_declaration=True)
    xml_str = xml_bytes.getvalue().decode("utf-8")
    # Parse with minidom and replace manga_title text with CDATA
    dom = minidom.parseString(xml_str)
    for elem in dom.getElementsByTagName("manga_title"):
        text = elem.firstChild.nodeValue if elem.firstChild else ''
        if elem.firstChild:
            elem.removeChild(elem.firstChild)
        cdata = dom.createCDATASection(text)
        elem.appendChild(cdata)
    # Write pretty xml to file
    with open(filename, "w", encoding="utf-8") as f:
        dom.writexml(f, encoding="utf-8")
    print(f"Exported {exported} manga to xml file.")
    # Ask user if they want to add unlinked manga with AL ID to AniList
    answer = input("Some manga might have AL ID. Do you want to add them to your AniList account? This requires an API Client. (y/n): ").strip().lower()
    if answer == 'y':
        sync_to_anilist(unlinked)
    elif answer == 'n':
        export_unlinked_to_csv(unlinked, filename='export/unlinked_by_MAL.csv')
    else:
        print("Invalid input. unlinked manga will not be added to AniList.")
        export_unlinked_to_csv(unlinked, filename='export/unlinked_by_MAL.csv')


def sync_to_anilist(mangas):
    """Sync manga with AL ID to AniList, rate-limited."""
    print("To use the AniList API, you need an API Client.")
    print("1. Go to https://anilist.co/settings/developer and create a new application.")
    print("2. Set the redirect URL to https://anilist.co/api/v2/oauth/pin.")
    manga_with_al_id = []
    manga_without_al_id = []
    for manga in mangas:
        attributes = manga.get('attributes', {})
        links = attributes.get('links')
        al_id = None
        if isinstance(links, dict):
            al_id = links.get('al')
        if al_id:
            manga_with_al_id.append(manga)
        else:
            manga_without_al_id.append(manga)
    if manga_with_al_id:
        access_token = anilist_authorization_code_flow()
        print(f"\nAdding {len(manga_with_al_id)} manga to AniList...")
        with Progress(
            SpinnerColumn(),
            TextColumn("{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
            transient=True,
        ) as progress:
            task = progress.add_task("Adding manga to AniList...", total=len(manga_with_al_id))
            for manga in manga_with_al_id:
                attributes = manga.get('attributes', {})
                links = attributes.get('links')
                al_id = None
                if isinstance(links, dict):
                    al_id = links.get('al')
                if not al_id:
                    progress.update(task, advance=1)
                    continue
                mutation = '''
mutation ($mediaId: Int!, $status: MediaListStatus, $score: Float, $progress: Int, $progressVolumes: Int) {
  SaveMediaListEntry(mediaId: $mediaId, status: $status, score: $score, progress: $progress, progressVolumes: $progressVolumes) {
    id
    status
    score
    progress
    progressVolumes
  }
}
'''
                reading_status_map = {
                    "reading": "CURRENT",
                    "completed": "COMPLETED",
                    "on_hold": "PAUSED",
                    "dropped": "DROPPED",
                    "plan_to_read": "PLANNING",
                    "re_reading": "CURRENT"
                }
                raw_status = manga.get('reading_status')
                mapped_status = reading_status_map.get(raw_status, "CURRENT")
                # Get score, progress (chapter), and progressVolumes (volume) if available
                score = manga.get('user_rating')
                try:
                    score = float(score) if score is not None else None
                except Exception:
                    score = None
                progress_chapter = None
                progress_volume = None
                try:
                    progress_chapter = int(float(manga.get('read_chapter', 0)))
                except Exception:
                    progress_chapter = None
                try:
                    progress_volume = int(float(manga.get('read_volume', 0)))
                except Exception:
                    progress_volume = None
                variables = {
                    "mediaId": int(al_id),
                    "status": mapped_status,
                    "score": score,
                    "progress": progress_chapter,
                    "progressVolumes": progress_volume
                }
                # Remove None values (AniList API does not accept them)
                variables = {k: v for k, v in variables.items() if v is not None}
                headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
                response = request_with_retry(
                    'POST',
                    "https://graphql.anilist.co",
                    json={"query": mutation, "variables": variables},
                    headers=headers
                )
                # Optionally, you can log failures to a file or list
                progress.update(task, advance=1)
                time.sleep(2.1)
        if manga_without_al_id:
            print(f"\n{len(manga_without_al_id)} manga without MAL or AL link will be exported to CSV.")
            export_unlinked_to_csv(manga_without_al_id, filename='export/unlinked.csv')
    else:
        print("No manga linked with AniList found to add to AniList.")
        export_unlinked_to_csv(mangas, filename='export/unlinked.csv')

# --- Language Helper ---
def iso6391_to_language(code):
    special = {
        'zh': 'Simplified Chinese',
        'zh-hk': 'Traditional Chinese',
        'pt-br': 'Brazilian Portuguese',
        'es': 'Castilian Spanish',
        'es-la': 'Latin American Spanish',
        'ja-ro': 'Romanized Japanese',
        'ko-ro': 'Romanized Korean',
        'zh-ro': 'Romanized Chinese',
    }
    iso_map = {
        'en': 'English', 'ja': 'Japanese', 'ko': 'Korean', 'zh': 'Chinese', 'fr': 'French', 'de': 'German',
        'es': 'Spanish', 'it': 'Italian', 'ru': 'Russian', 'pt': 'Portuguese', 'pl': 'Polish', 'id': 'Indonesian',
        'tr': 'Turkish', 'ar': 'Arabic', 'th': 'Thai', 'vi': 'Vietnamese', 'cs': 'Czech', 'ms': 'Malay',
        'ro': 'Romanian', 'uk': 'Ukrainian', 'hu': 'Hungarian', 'bg': 'Bulgarian', 'fa': 'Persian', 'he': 'Hebrew',
        'hi': 'Hindi', 'bn': 'Bengali', 'el': 'Greek', 'sv': 'Swedish', 'fi': 'Finnish', 'da': 'Danish',
        'no': 'Norwegian', 'nl': 'Dutch', 'ca': 'Catalan', 'sr': 'Serbian', 'hr': 'Croatian', 'sk': 'Slovak',
        'sl': 'Slovenian', 'et': 'Estonian', 'lv': 'Latvian', 'lt': 'Lithuanian', 'ta': 'Tamil', 'te': 'Telugu',
        'ml': 'Malayalam', 'kn': 'Kannada', 'mr': 'Marathi', 'gu': 'Gujarati', 'pa': 'Punjabi', 'ur': 'Urdu',
        'my': 'Burmese', 'km': 'Khmer', 'lo': 'Lao', 'si': 'Sinhala', 'am': 'Amharic', 'sw': 'Swahili',
        'zu': 'Zulu', 'xh': 'Xhosa', 'st': 'Southern Sotho', 'tn': 'Tswana', 'ts': 'Tsonga', 'ss': 'Swati',
        've': 'Venda', 'nr': 'Southern Ndebele', 'nd': 'Northern Ndebele', 'af': 'Afrikaans', 'sq': 'Albanian',
        'bs': 'Bosnian', 'mk': 'Macedonian', 'mt': 'Maltese', 'ga': 'Irish', 'cy': 'Welsh', 'gd': 'Scottish Gaelic',
        'br': 'Breton', 'eu': 'Basque', 'gl': 'Galician', 'oc': 'Occitan', 'lb': 'Luxembourgish', 'is': 'Icelandic',
        'fo': 'Faroese', 'kl': 'Greenlandic', 'sm': 'Samoan', 'to': 'Tongan', 'fj': 'Fijian', 'mi': 'Maori',
        'qu': 'Quechua', 'ay': 'Aymara', 'gn': 'Guarani', 'tt': 'Tatar', 'ba': 'Bashkir', 'cv': 'Chuvash',
        'ce': 'Chechen', 'os': 'Ossetian', 'av': 'Avaric', 'kv': 'Komi', 'cu': 'Church Slavic', 'tk': 'Turkmen',
        'ky': 'Kyrgyz', 'kk': 'Kazakh', 'uz': 'Uzbek', 'tg': 'Tajik', 'mn': 'Mongolian', 'ne': 'Nepali',
        'si': 'Sinhala', 'ps': 'Pashto', 'sd': 'Sindhi', 'ug': 'Uyghur', 'uz': 'Uzbek', 'kk': 'Kazakh',
        'ky': 'Kyrgyz', 'tk': 'Turkmen', 'az': 'Azerbaijani', 'ka': 'Georgian', 'hy': 'Armenian', 'ab': 'Abkhazian',
        'os': 'Ossetian', 'cv': 'Chuvash', 'ba': 'Bashkir', 'tt': 'Tatar', 'sah': 'Yakut', 'ce': 'Chechen',
        'cu': 'Church Slavic', 'cv': 'Chuvash', 'kv': 'Komi', 'av': 'Avaric', 'ae': 'Avestan', 'nr': 'Southern Ndebele',
        'ss': 'Swati', 'st': 'Southern Sotho', 'tn': 'Tswana', 'ts': 'Tsonga', 've': 'Venda', 'xh': 'Xhosa', 'zu': 'Zulu',
    }
    if code in special:
        return special[code]
    if code in iso_map:
        return iso_map[code]
    return code

# --- Export Orchestration ---
def export_all(manga_info, choices, session=None):
    if '1' in choices:
        export_manga_list(manga_info, "xml", "export/manga_library.xml", session=session)
    if '2' in choices:
        export_manga_list(manga_info, "json", "export/manga_library.json")
    if '3' in choices:
        export_manga_list(manga_info, "csv", "export/manga_library.csv")

# --- Export Dispatcher ---
def export_manga_list(manga_info_list, export_format, filename, session=None):
    if export_format == "xml":
        export_manga_list_to_xml(manga_info_list, filename, session=session)
    elif export_format == "json":
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(manga_info_list, f, ensure_ascii=False, indent=2)
        print(f"Exported {len(manga_info_list)} manga to {filename}")
    elif export_format == "csv":
        export_manga_list_to_csv(manga_info_list, filename)
    else:
        print(f"Unsupported format: {export_format}")

def export_manga_list_to_csv(manga_info_list, filename="manga_library.csv"):
    columns = [
        "MAL Id", "AL Id", "Type", "Title", "Description", "Original Language", "Demographic", "Status", "Year", "Content Rating", "Tags", "Author", "Artist", "Reading Status"
    ]
    reading_status_map = {
        "reading": "Reading",
        "completed": "Completed",
        "on_hold": "On-Hold",
        "dropped": "Dropped",
        "plan_to_read": "Plan to Read",
        "re_reading": "Reading"
    }
    os.makedirs(os.path.dirname(filename), exist_ok=True)
    with open(filename, "w", encoding="utf-8", newline='') as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(columns)
        for manga in manga_info_list:
            attributes = manga.get('attributes', {})
            links = attributes.get('links', {})
            mal_id = links.get('mal', '-') if isinstance(links, dict) else '-'
            al_id = links.get('al', '-') if isinstance(links, dict) else '-'
            manga_type = manga.get('type', '').capitalize() if manga.get('type') else ''
            title_dict = attributes.get('title', {})
            title = title_dict.get('en') or next(iter(title_dict.values()), '')
            description = attributes.get('description', {}).get('en', '')
            orig_lang = iso6391_to_language(attributes.get('originalLanguage', ''))
            demographic = attributes.get('publicationDemographic', '')
            demographic = demographic.capitalize() if demographic else ''
            status = attributes.get('status', '')
            status = status.capitalize() if status else ''
            year = attributes.get('year', '')
            content_rating = attributes.get('contentRating', '')
            content_rating = content_rating.capitalize() if content_rating else ''
            tags = ', '.join([t['attributes']['name'].get('en', '') for t in manga.get('attributes', {}).get('tags', []) if 'attributes' in t and 'name' in t['attributes']])
            author = ''
            artist = ''
            for rel in manga.get('relationships', []):
                if rel.get('type') == 'author':
                    author = rel.get('attributes', {}).get('name', author)
                if rel.get('type') == 'artist':
                    artist = rel.get('attributes', {}).get('name', artist)
            reading_status_raw = manga.get('reading_status', attributes.get('reading_status', ''))
            reading_status = reading_status_map.get(reading_status_raw, reading_status_raw.capitalize() if reading_status_raw else '')
            writer.writerow([
                mal_id, al_id, manga_type, title, description, orig_lang, demographic, status, year, content_rating, tags, author, artist, reading_status
            ])
    print(f"Exported {len(manga_info_list)} manga to {filename}")

def request_with_retry(method, url, max_retries=6, delay=10, **kwargs):
    """Make a requests call with retry logic for network errors and re-login on 401."""
    global SESSION_TOKENS
    for attempt in range(max_retries):
        try:
            response = requests.request(method, url, **kwargs)
            if response.status_code == 401:
                # Try to re-login if credentials are available
                credentials = load_credentials() if 'LOGIN_ENDPOINT' not in url else None
                if credentials:
                    print("Session expired (401). Attempting to re-login...")
                    username, password = credentials
                    try:
                        tokens = login(username, password)
                        save_session(tokens, credentials)
                        # Update Authorization header if present
                        if 'headers' in kwargs and kwargs['headers']:
                            kwargs['headers']['Authorization'] = f"Bearer {tokens['token']['session']}"
                        else:
                            kwargs['headers'] = {"Authorization": f"Bearer {tokens['token']['session']}"}
                        # Retry the request once after re-login
                        response = requests.request(method, url, **kwargs)
                        response.raise_for_status()
                        return response
                    except Exception as e:
                        print(f"Re-login failed: {e}")
                        raise
                else:
                    print("401 Unauthorized and no credentials available for re-login.")
                    response.raise_for_status()
            response.raise_for_status()
            return response
        except (ConnectionError, Timeout) as e:
            if attempt < max_retries - 1:
                print(f"Request failed ({e}), retrying in {delay} seconds... [{attempt+1}/{max_retries}]")
                time.sleep(delay)
            else:
                print(f"Request failed after {max_retries} attempts: {e}")
                raise
        except HTTPError as e:
            print(f"HTTP error: {e}")
            raise

# --- Main Menu ---
def import_to_anilist_then_export(manga_info, session=None):
    # Split manga by AL ID
    manga_with_alid = []
    manga_with_malid = []
    manga_unlinked = []
    for manga in manga_info:
        attributes = manga.get('attributes', {})
        links = attributes.get('links', {})
        al_id = links.get('al') if isinstance(links, dict) else None
        mal_id = links.get('mal') if isinstance(links, dict) else None
        if al_id:
            manga_with_alid.append(manga)
        elif mal_id:
            manga_with_malid.append(manga)
        else:
            manga_unlinked.append(manga)
    # Add to AniList
    if manga_with_alid:
        sync_to_anilist(manga_with_alid)
    # Export remaining
    if manga_with_malid:
        export_manga_list_to_xml(manga_with_malid, filename="export/manga_library.xml", session=session)
    if manga_unlinked:
        export_unlinked_to_csv(manga_unlinked, filename='export/unlinked.csv')

def main():
    while True:
        print("Select export option:")
        print("1. Import to AniList (Requires AniList API Client & take a long time)")
        print("2. Export MAL XML")
        print("3. Export JSON")
        print("4. Export CSV")
        print("5. Logout")
        print("q. Quit")
        choice = input("Choice (comma separated for multiple): ").strip()
        choices = [c.strip() for c in choice.split(',') if c.strip()]
        valid_choices = {'1', '2', '3', '4', '5', 'q'}
        if not all(c in valid_choices for c in choices):
            print("Invalid choice. Please try again.")
            continue
        if 'q' in choices:
            break
        if '5' in choices:
            global SESSION_TOKENS
            SESSION_TOKENS = None
            print("Logged out.")
            continue
        # Warn about AniList API Client before fetching manga info
        if '1' in choices:
            print("\nWARNING: This function requires an AniList API Client (Client ID & Secret) and will take a long time due to rate limits.")
            answer = input("Do you want to continue? (y/n): ").strip().lower()
            if answer != 'y':
                print("Returning to main menu.")
                continue
        session, tokens = ensure_valid_session()
        manga_info = fetch_and_prepare_manga_info(session)
        if '1' in choices:
            import_to_anilist_then_export(manga_info, session=session)
        if any(c in {'2', '3', '4'} for c in choices):
            export_all(manga_info, choices, session=session)

if __name__ == "__main__":
    main()

