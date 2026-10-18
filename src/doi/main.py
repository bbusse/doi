import csv
import io
import ipaddress
import logging
import json
import os
import platform
import random
import select
import socket
import sqlite3
import struct
import subprocess
import threading
import time
import tempfile
import xml.etree.ElementTree as ET

__version__ = "0.1.0"
import requests
from paho.mqtt import client as mqtt_client

from doi.cache import FileCache, cache  # noqa: F401

USER_AGENT = f"doi/{__version__} (https://github.com/bbusse/doi)"


def skip_comments(file):
    for line in file:
        if not line.strip().startswith('#'):
            yield line.strip()


# Content-Type first, then the magic bytes, so the cached file's extension
# matches what a viewer will find inside it. A webp saved as .jpg trips up
# loaders that trust the name
_IMAGE_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
              "image/webp": ".webp", "image/avif": ".avif",
              "image/svg+xml": ".svg", "image/bmp": ".bmp",
              "image/tiff": ".tiff"}

# Types that say nothing about the body, so the bytes decide
_GENERIC_TYPES = ("", "application/octet-stream", "binary/octet-stream")


def _media_type(content_type):
    return (content_type or "").split(";", 1)[0].strip().lower()


def _sniff_image(content):
    if content[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if content[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return ".webp"
    if content[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    head = content[:64].lstrip()
    if head[:5] == b"<?xml" or head[:4] == b"<svg":
        return ".svg"

    return None


def image_extension(content_type, content):
    return (_IMAGE_EXT.get(_media_type(content_type))
            or _sniff_image(content) or ".jpg")


def is_image(content_type, content):
    if not content:
        return False
    media_type = _media_type(content_type)
    if media_type.startswith("image/"):
        return True

    return media_type in _GENERIC_TYPES and _sniff_image(content) is not None


def cache_image(url, store=None, timeout=10):
    '''
    A picture by url as a local file, fetched once and kept in the store, the
    shared cache unless one is given. The extension follows the response
    Content-Type. Empty string on no url, a failed fetch or a body that is
    not an image, so an error page is never cached as a picture
    '''
    if not url:
        return ""

    store = store or cache
    path = store.path(url)
    if path:
        return path

    try:
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type")
        if not is_image(content_type, resp.content):
            logging.warning(f"cache_image: {url}: not an image "
                            f"({content_type or 'no content type'})")
            return ""

        return store.put(url, resp.content,
                         image_extension(content_type, resp.content))
    except (requests.RequestException, OSError) as e:
        logging.warning(f"cache_image: {url}: {e}")
        return ""


class APOD:

    def __init__(self, api_key="DEMO_KEY", store=None):
        self.api_key = api_key
        self.store = store or cache

    def apod_data(self):
        """
        Downloads NASA's Astronomy Picture of the Day (APOD).
        Returns:
            tuple: (image_path, meta) or (None, None) on failure. meta is the
            same shape the art sources return, so Art.caption formats it:
            title, artist (the credit), date, source and a description.
        """
        # thumbs=True asks the api for a poster frame on the days the picture
        # is actually a video
        apod_url = (f"https://api.nasa.gov/planetary/apod?api_key={self.api_key}"
                    "&thumbs=True")
        try:
            resp = requests.get(apod_url, timeout=10)
            if resp.status_code != 200:
                logging.error(f"APOD: Failed to fetch metadata: {resp.status_code}")
                return None, None
            data = resp.json()

            meta = {"title": data.get("title", ""),
                    "artist": (data.get("copyright") or "").strip(),
                    "date": data.get("date", ""),
                    "source": "NASA APOD",
                    "description": data.get("explanation", "")}

            if data.get("media_type", "image") == "image":
                img_url = data.get("hdurl") or data.get("url")
            else:
                # video or other: the api's thumbnail if it gave one
                img_url = data.get("thumbnail_url")
            if not img_url:
                logging.warning("APOD: no image today "
                                f"(media_type={data.get('media_type')})")
                return None, meta

            img_resp = requests.get(img_url, timeout=10)
            if img_resp.status_code != 200:
                logging.error(f"APOD: Failed to download image: {img_resp.status_code}")
                return None, meta
            content_type = img_resp.headers.get("Content-Type", "")
            if content_type and not content_type.startswith("image/"):
                logging.warning(f"APOD: {img_url} served {content_type}, skipping")
                return None, meta

            img_ext = os.path.splitext(img_url.split("?", 1)[0])[-1] or ".jpg"

            return self.store.put(img_url, img_resp.content, img_ext), meta
        except Exception as e:
            logging.error(f"APOD: Error fetching APOD: {e}")
            return None, None


class Art:
    """
    Base for the collection sources. Each one picks a work at random and
    returns a local image path plus its meta: title, artist, date, source
    """

    def __init__(self, store=None, width=1280, height=800):
        self.store = store or cache
        self.width = width
        self.height = height

    def save_image(self, url):
        return cache_image(url, self.store, timeout=20) or None

    def art_data(self):
        raise NotImplementedError


class ArtNGA(Art):
    """
    The National Gallery of Art, from its open data.

    There is no api, only csv, and the file naming the images is 85 MB, far too
    much to pull for one picture. It is served with range support though, so a
    read of a few kilobytes at a random offset lands in the middle of some row,
    and the first complete row after that is as good as any other.
    """

    images_csv = ("https://raw.githubusercontent.com/NationalGalleryOfArt/"
                  "opendata/main/data/published_images.csv")
    iiif = "https://api.nga.gov/iiif/{uuid}/full/!{width},{height}/0/default.jpg"
    window = 8192
    # Without this the server gzips, content-length describes the compressed
    # file and a byte range lands in the middle of a gzip stream, which is not
    # itself decompressible
    plain = {"Accept-Encoding": "identity"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._size = None
        self._objects_size = None

    def csv_size(self):
        if self._size is None:
            r = requests.head(self.images_csv, timeout=10,
                              allow_redirects=True, headers=self.plain)
            r.raise_for_status()
            self._size = int(r.headers["content-length"])

        return self._size

    def random_row(self):
        size = self.csv_size()
        start = random.randint(self.window, max(size - self.window, self.window + 1))
        headers = dict(self.plain)
        headers["Range"] = f"bytes={start}-{start + self.window}"
        r = requests.get(self.images_csv, timeout=15, headers=headers)
        r.raise_for_status()
        # The read starts mid-row, so the first line is a fragment
        for line in r.text.splitlines()[1:]:
            fields = next(csv.reader([line]), [])
            # uuid, iiifurl, iiifthumburl, viewtype, sequence, width, height,
            # maxpixels, openaccess, created, modified, tmsid, assistivetext
            if len(fields) < 13 or fields[3] != "primary" or fields[8] != "1":
                continue
            if not fields[0] or "-" not in fields[0]:
                continue

            return {"uuid": fields[0],
                    "objectid": fields[11],
                    "text": fields[12]}

        return None

    objects_csv = ("https://raw.githubusercontent.com/NationalGalleryOfArt/"
                   "opendata/main/data/objects.csv")
    object_fields = 31
    max_probes = 30

    def objects_size(self):
        if self._objects_size is None:
            r = requests.head(self.objects_csv, timeout=10,
                              allow_redirects=True, headers=self.plain)
            r.raise_for_status()
            self._objects_size = int(r.headers["content-length"])

        return self._objects_size

    def object_row(self, text):
        lines = text.splitlines()[1:]
        for i, line in enumerate(lines):
            head = next(csv.reader([line]), [])
            if not head or not head[0].isdigit():
                continue
            for row in csv.reader(io.StringIO("\n".join(lines[i:]))):
                if len(row) >= self.object_fields and row[0].isdigit():
                    return row
                break

        return None

    def object_meta(self, objectid):
        try:
            wanted = int(objectid)
        except (TypeError, ValueError):
            return {}

        lo, hi, probes = 0, self.objects_size(), 0
        while lo < hi and probes < self.max_probes:
            mid = (lo + hi) // 2
            probes += 1
            headers = dict(self.plain)
            headers["Range"] = f"bytes={mid}-{mid + self.window}"
            r = requests.get(self.objects_csv, timeout=15, headers=headers)
            r.raise_for_status()
            row = self.object_row(r.text)
            if row is None:
                lo = mid + self.window
                continue

            found = int(row[0])
            if found == wanted:
                return {"title": row[5], "date": row[6],
                        "medium": row[10], "artist": row[15]}
            if found < wanted:
                lo = mid + 1
            else:
                hi = mid

        logging.info(f"Art: No metadata for object {objectid} in {probes} reads")

        return {}

    def art_data(self):
        try:
            row = self.random_row()
            if not row:
                logging.error("Art: No usable row in the sampled range")

                return None, None

            url = self.iiif.format(uuid=row["uuid"], width=self.width,
                                   height=self.height)
            path = self.save_image(url)
            meta = self.object_meta(row["objectid"])
            meta["description"] = row["text"] or ""
            meta["source"] = "National Gallery of Art"

            return path, meta
        except Exception as e:
            logging.error(f"Art: Error fetching from the National Gallery: {e}")

            return None, None


class ArtMet(Art):
    """
    The Metropolitan Museum of Art, from its public api.

    The search returns every object id that carries an image in one response,
    so a random pick needs that list and then one object lookup.
    """

    api = "https://collectionapi.metmuseum.org/public/collection/v1"
    search_terms = ("painting", "drawing", "sculpture", "portrait",
                    "landscape", "still life")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._ids = []

    def object_ids(self):
        if not self._ids:
            term = random.choice(self.search_terms)
            r = requests.get(f"{self.api}/search",
                             params={"hasImages": "true", "q": term},
                             timeout=15)
            r.raise_for_status()
            self._ids = r.json().get("objectIDs") or []

        return self._ids

    def random_object(self, tries=5):
        ids = self.object_ids()
        if not ids:
            return None

        for _ in range(tries):
            r = requests.get(f"{self.api}/objects/{random.choice(ids)}",
                             timeout=15)
            if r.status_code != 200:
                continue
            data = r.json()
            # web-large is a fraction of the original and still generously sized
            image = data.get("primaryImageSmall") or data.get("primaryImage")
            if image:
                return {"image": image,
                        "title": data.get("title") or "",
                        "artist": data.get("artistDisplayName") or "",
                        "date": data.get("objectDate") or "",
                        "medium": data.get("medium") or "",
                        "description": data.get("creditLine") or ""}

        return None

    def art_data(self):
        try:
            work = self.random_object()
            if not work:
                logging.error("Art: No object with an image found")

                return None, None

            path = self.save_image(work["image"])
            work["source"] = "The Metropolitan Museum of Art"

            return path, work
        except Exception as e:
            logging.error(f"Art: Error fetching from the Met: {e}")

            return None, None


class Music:

    spotify_token_url = "https://accounts.spotify.com/api/token"
    spotify_playing_url = ("https://api.spotify.com/v1/me/player"
                           "/currently-playing")

    def __init__(self, store=None):
        self.store = store or cache
        self.music = {'mpd_data'   : False,
                      'mpd_state'  : False,
                      'mpd_artist' : "",
                      'mpd_title'  : "",
                      'mpd_album'  : ""}
        self.spotify_access_token = ""
        self.spotify_token_expiry = 0

    def mpd(self):
        mpd = Py3status("mpd")
        data = mpd.run_module()
        return data

    @staticmethod
    def spotify_configured():
        return bool(os.environ.get("SPOTIFY_CLIENT_ID")
                    and os.environ.get("SPOTIFY_CLIENT_SECRET")
                    and os.environ.get("SPOTIFY_REFRESH_TOKEN"))

    def spotify_token(self):
        '''
        A valid access token, refreshed from SPOTIFY_REFRESH_TOKEN when the
        cached one has expired. Empty when unconfigured or refused.
        '''
        if self.spotify_access_token and \
           time.monotonic() < self.spotify_token_expiry:
            return self.spotify_access_token

        if not self.spotify_configured():
            return ""

        try:
            response = requests.post(
                self.spotify_token_url,
                data={"grant_type": "refresh_token",
                      "refresh_token":
                          os.environ["SPOTIFY_REFRESH_TOKEN"]},
                auth=(os.environ["SPOTIFY_CLIENT_ID"],
                      os.environ["SPOTIFY_CLIENT_SECRET"]),
                timeout=10)
        except requests.RequestException as e:
            logging.warning(f"spotify: token refresh failed: {e}")
            return ""

        if response.status_code != 200:
            logging.warning("spotify: token refresh failed: "
                            f"HTTP {response.status_code}")
            return ""

        data = response.json()
        self.spotify_access_token = data.get("access_token", "")
        self.spotify_token_expiry = time.monotonic() \
            + data.get("expires_in", 3600) - 60

        return self.spotify_access_token

    def spotify(self):
        '''
        What the account is playing right now.
        Returns None when unconfigured or on error, an empty dict when
        nothing is playing, and otherwise the track: title, artists, album,
        url, art_file, is_playing, progress_ms, duration_ms.
        '''
        token = self.spotify_token()
        if not token:
            return None

        try:
            response = requests.get(
                self.spotify_playing_url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=10)
        except requests.RequestException as e:
            logging.warning(f"spotify: currently-playing failed: {e}")
            return None

        # 204 is the authenticated answer for silence
        if response.status_code == 204:
            return {}
        if response.status_code != 200:
            logging.warning("spotify: currently-playing failed: "
                            f"HTTP {response.status_code}")
            return None

        data = response.json()
        item = data.get("item") or {}
        if not item:
            return {}

        album = item.get("album") or {}
        # The images come largest first; the mid size balances detail
        # against transfer
        images = album.get("images") or []
        art_url = images[1].get("url", "") if len(images) > 1 \
            else (images[0].get("url", "") if images else "")

        return {"title": item.get("name", ""),
                "artists": ", ".join(a.get("name", "")
                                     for a in item.get("artists") or []),
                "album": album.get("name", ""),
                "url": (item.get("external_urls") or {}).get("spotify", ""),
                "art_file": cache_image(art_url, self.store),
                "is_playing": data.get("is_playing", False),
                "progress_ms": data.get("progress_ms") or 0,
                "duration_ms": item.get("duration_ms") or 0}


class Calendar:

    @staticmethod
    def next_events_today(max_results=5, calendar_id='primary', credentials_path=None):
        """
        Fetches the next `max_results` events for the current day from Google Calendar.
        Args:
            max_results (int): Number of events to return.
            calendar_id (str): Google Calendar ID (default: 'primary').
            credentials_path (str): Path to Google API credentials.json/token.json. If None, uses default location.
        Returns:
            list of dicts: [{ 'start': ..., 'end': ..., 'summary': ... }, ...] or None on failure.
        """
        try:
            from datetime import datetime, timedelta
            import os
            import pytz
            from googleapiclient.discovery import build
            from google.oauth2.credentials import Credentials
            # Set up credentials
            if credentials_path is None:
                credentials_path = os.path.expanduser('~/.config/gcalendar/token.json')
            creds = Credentials.from_authorized_user_file(credentials_path, ['https://www.googleapis.com/auth/calendar.readonly'])
            service = build('calendar', 'v3', credentials=creds)
            # Get time range for today in UTC
            tz = pytz.UTC
            now = datetime.now(tz)
            start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
            end_of_day = start_of_day + timedelta(days=1)
            time_min = start_of_day.isoformat()
            time_max = end_of_day.isoformat()
            events_result = service.events().list(
                calendarId=calendar_id,
                timeMin=time_min,
                timeMax=time_max,
                maxResults=max_results,
                singleEvents=True,
                orderBy='startTime'
            ).execute()
            events = events_result.get('items', [])
            result = []
            for event in events:
                start = event['start'].get('dateTime', event['start'].get('date'))
                end = event['end'].get('dateTime', event['end'].get('date'))
                summary = event.get('summary', '')
                result.append({'start': start, 'end': end, 'summary': summary})
            return result
        except Exception as e:
            logging.error(f"Google Calendar fetch failed: {e}")
            return None

    @staticmethod
    def month(today=None):
        '''
        The current month as data: year, month and its name, the weekday
        abbreviations, the weeks as rows of day numbers with 0 outside the
        month, and today's day
        '''
        import calendar
        import datetime

        today = today or datetime.date.today()
        cal = calendar.Calendar(calendar.MONDAY)

        return {"year": today.year,
                "month": today.month,
                "month_name": calendar.month_name[today.month],
                "weekdays": [calendar.day_abbr[d] for d in cal.iterweekdays()],
                "weeks": cal.monthdayscalendar(today.year, today.month),
                "today": today.day}

    @staticmethod
    def next_bank_holidays(location="Germany", count=3):
        """
        Fetches the next `count` bank holidays for the given location using the Nager.Date API.
        Returns:
            list of dicts: [{ "date": "YYYY-MM-DD", "localName": "Holiday Name", "name": "English Name" }, ...]
            or None on failure.
        """
        # Map some common location names to country codes for Nager.Date API
        country_map = {
            "Germany": "DE",
            "DE": "DE",
            "United Kingdom": "GB",
            "UK": "GB",
            "Great Britain": "GB",
            "France": "FR",
            "FR": "FR",
            "United States": "US",
            "USA": "US",
            "US": "US",
            "Austria": "AT",
            "AT": "AT",
            "Switzerland": "CH",
            "CH": "CH",
        }
        import datetime
        today = datetime.date.today()
        year = today.year
        country_code = country_map.get(location, location)
        # Validate country code (ISO 3166-1 alpha-2 expected)
        if not isinstance(country_code, str) or len(country_code) != 2 or not country_code.isalpha():
            logging.error(f"Bank holidays: invalid country '{location}' -> '{country_code}'")
            return None
        country_code = country_code.upper()
        url = f"https://date.nager.at/api/v3/PublicHolidays/{year}/{country_code}"
        try:
            resp = requests.get(url, timeout=10)
            if resp.status_code != 200:
                logging.error(f"Failed to fetch bank holidays ({country_code}) for {year}: HTTP {resp.status_code}")
                return None
            try:
                holidays = resp.json()
            except Exception as e:
                logging.error(f"Failed to decode bank holidays JSON: {e}")
                return None
            # Filter for holidays after today
            upcoming = [
                h for h in holidays
                if datetime.datetime.strptime(h["date"], "%Y-%m-%d").date() >= today
            ]
            # If not enough holidays left this year, fetch next year as well
            if len(upcoming) < count:
                url_next = f"https://date.nager.at/api/v3/PublicHolidays/{year+1}/{country_code}"
                resp_next = requests.get(url_next, timeout=10)
                if resp_next.status_code == 200:
                    try:
                        holidays_next = resp_next.json()
                    except Exception as e:
                        logging.error(f"Failed to decode bank holidays JSON (next year): {e}")
                        holidays_next = []
                else:
                    logging.warning(f"Bank holidays next year fetch returned HTTP {resp_next.status_code}")
                    holidays_next = []
                upcoming += holidays_next
                # Filter again for only future holidays
                upcoming = [
                    h for h in upcoming
                    if datetime.datetime.strptime(h["date"], "%Y-%m-%d").date() >= today
                ]
            # Return the next `count` holidays
            return [
                {
                    "date": h["date"],
                    "localName": h["localName"],
                    "name": h["name"]
                }
                for h in upcoming[:count]
            ]
        except Exception as e:
            logging.error(f"Failed to fetch bank holidays: {e}")
            return None


    @staticmethod
    def moonphase(location="Berlin"):
        """
        Fetches the current moon phase and icon from wttr.in for the given location.
        Returns:
            tuple: (moon_phase_text, moon_icon_url, days_until_full_moon) or (None, None, None) on failure.
        """
        import datetime

        url = f"https://wttr.in/{location}?format=j1"
        try:
            resp = requests.get(url, timeout=10)
            data = resp.json()
            # The moon phase is in the first day's astronomy section
            moon_phase = data["weather"][0]["astronomy"][0]["moon_phase"]
            moon_icon = data["weather"][0]["astronomy"][0].get("moon_icon", None)
            # wttr.in does not always provide a direct icon URL, so we map phase to icon if needed
            moon_icon_map = {
                "New Moon": "new-moon",
                "Waxing Crescent": "waxing-crescent",
                "First Quarter": "first-quarter",
                "Waxing Gibbous": "waxing-gibbous",
                "Full Moon": "full-moon",
                "Waning Gibbous": "waning-gibbous",
                "Last Quarter": "last-quarter",
                "Waning Crescent": "waning-crescent"
            }
            if not moon_icon:
                icon_name = moon_icon_map.get(moon_phase, "moon")
                moon_icon = f"https://wttr.in/files/{icon_name}.png"

            # Calculate days until next full moon
            today = datetime.date.today()
            days_until_full_moon = None
            # Look ahead in the weather forecast for the next full moon
            for day in data.get("weather", []):
                astronomy = day.get("astronomy", [])
                if astronomy and astronomy[0].get("moon_phase") == "Full Moon":
                    date_str = day.get("date")
                    if date_str:
                        date_obj = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
                        delta = (date_obj - today).days
                        if delta >= 0:
                            days_until_full_moon = delta
                            break
            return moon_phase, moon_icon, days_until_full_moon
        except Exception as e:
            logging.error(f"Failed to fetch moonphase: {e}")
            return None, None, None


    @staticmethod
    def sunrise_sunset(location="Berlin"):
        """
        Fetches sunrise and sunset times for today and tomorrow from wttr.in for the given location.
        Returns:
            dict: {
                "today": {"sunrise": str, "sunset": str},
                "tomorrow": {"sunrise": str, "sunset": str}
            }
            or None on failure.
        """
        url = f"https://wttr.in/{location}?format=j1"
        try:
            resp = requests.get(url, timeout=10)
            data = resp.json()
            result = {}
            weather = data.get("weather", [])
            for idx, key in zip([0, 1], ["today", "tomorrow"]):
                if idx < len(weather):
                    astronomy = weather[idx].get("astronomy", [{}])[0]
                    sunrise = astronomy.get("sunrise", None)
                    sunset = astronomy.get("sunset", None)
                    result[key] = {"sunrise": sunrise, "sunset": sunset}
            return result
        except Exception as e:
            logging.error(f"Failed to fetch sunrise/sunset: {e}")
            return None


class MQTT:

    def __init__(self, broker, client_id, port=1883, user="", pw=""):
        self.client_id = client_id
        self.broker = broker
        self.port = port
        self.user = user
        self.pw = pw

    def connect(self):
        # paho-mqtt v2 callback signature support
        def on_connect(client, userdata, flags, reason_code, properties=None):
            if reason_code == 0:
                logging.info("MQTT: Connected")
            else:
                logging.error(f"MQTT: Failed to connect: {reason_code}")

        # Set client ID with v2 callback API when available
        try:
            if hasattr(mqtt_client, 'CallbackAPIVersion'):
                client = mqtt_client.Client(
                    client_id=self.client_id,
                    callback_api_version=mqtt_client.CallbackAPIVersion.V2
                )
            else:
                client = mqtt_client.Client(self.client_id)
        except TypeError:
            # Fallback for older client signatures
            client = mqtt_client.Client(self.client_id)

        if self.user != "" and self.pw != "":
            client.username_pw_set(self.user, self.pw)

        client.on_connect = on_connect
        client.connect(self.broker, self.port)
        return client

    def subscribe(self, client: mqtt_client, topic, on_message_callback):
        def on_message(client, userdata, msg):
            try:
                jm = msg.payload.decode(errors='ignore')
            except Exception:
                jm = ""
            try:
                m = json.loads(jm)
            except ValueError:
                m = None

            logging.info(f"Received `{m if m is not None else jm}` in  `{msg.topic}`")
            on_message_callback(msg.topic or "", m if m is not None else jm)

        client.subscribe(topic)
        # Register per-topic callback without clobbering global on_message
        client.message_callback_add(topic, on_message)


class News:

    def __init__(self, sources):
        self.news = []
        if sources.get("rss"):
            self.rss_fetch(sources["rss"])
        elif sources.get("db"):
            self.sqlite_select(sources["db"], "SELECT feeds.title, entries.title, entries.link, entries.pub_date FROM entries INNER JOIN feeds ON entries.feed_id = feeds.id ORDER BY pub_date DESC LIMIT 9")

    # news_item returns a single news text
    # from the previously fetched ones
    def news_item(self):
        n = {"feed": "",
             "title": "",
             "url": ""}
        if not self.news:
            return None
        try:
            n["feed"] = self.news[0].get('feed')
            n["title"] = self.news[0].get('title')
            n["url"] = self.news[0].get('url')
        finally:
            # Remove the item even if some keys are missing
            try:
                self.news.pop(0)
            except Exception:
                pass
        return n

    def sqlite_select(self, db, query):
        if not os.path.exists(db):
            logging.error(f"News: Database does not exist {db}")
            return False

        con = None
        try:
            con = sqlite3.connect(db)
            cur = con.cursor()
            res = cur.execute(query)
            r = res.fetchall()
            for news in r:
                logging.info("News: Appending news")
                self.news.append({
                    "feed": news[0],
                    "title": news[1],
                    "url": news[2]
                })
        except Exception as e:
            logging.error(f"News: sqlite error: {e}")
            return False
        finally:
            if con:
                con.close()

    def rss_fetch(self, url):
        ATOM_NS = "http://www.w3.org/2005/Atom"
        try:
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
        except Exception as e:
            logging.error(f"RSS: Failed to fetch {url}: {e}")
            return

        # RSS 2.0
        channel = root.find("channel")
        if channel is not None:
            feed_title = (channel.findtext("title") or "").strip()
            for item in channel.findall("item"):
                title = (item.findtext("title") or "").strip()
                link = (item.findtext("link") or "").strip()
                if title:
                    self.news.append({"feed": feed_title, "title": title, "url": link})
        else:
            # Atom
            feed_title = (root.findtext(f"{{{ATOM_NS}}}title") or "").strip()
            for entry in root.findall(f"{{{ATOM_NS}}}entry"):
                title = (entry.findtext(f"{{{ATOM_NS}}}title") or "").strip()
                link_el = entry.find(f"{{{ATOM_NS}}}link")
                link = (link_el.get("href", "") if link_el is not None else "").strip()
                if title:
                    self.news.append({"feed": feed_title, "title": title, "url": link})

        random.shuffle(self.news)
        logging.info(f"RSS: Fetched {len(self.news)} items from {url}")

class RSSFeed:
    ATOM_NS = "http://www.w3.org/2005/Atom"

    def __init__(self, url):
        self.url = url
        self._items = []
        self._fetched = False
        self._lock = threading.Lock()

    def item_count(self):
        return len(self._items)

    # Fetched on first use rather than in __init__, so building a playlist
    # never blocks on the network
    def fetch(self):
        with self._lock:
            if self._fetched:
                return
            self._fetched = True
            self._parse()

    def _parse(self):
        url = self.url
        try:
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
            channel = root.find("channel")
            if channel is not None:
                feed_title = (channel.findtext("title") or "").strip()
                for item in channel.findall("item"):
                    title = (item.findtext("title") or "").strip()
                    link = (item.findtext("link") or "").strip()
                    if title:
                        self._items.append({"feed": feed_title, "title": title, "url": link})
            else:
                feed_title = (root.findtext(f"{{{self.ATOM_NS}}}title") or "").strip()
                for entry in root.findall(f"{{{self.ATOM_NS}}}entry"):
                    title = (entry.findtext(f"{{{self.ATOM_NS}}}title") or "").strip()
                    link_el = entry.find(f"{{{self.ATOM_NS}}}link")
                    link = (link_el.get("href", "") if link_el is not None else "").strip()
                    if title:
                        self._items.append({"feed": feed_title, "title": title, "url": link})
            # Feed order is the ranking, so it is recorded before shuffling
            for rank, item in enumerate(self._items, start=1):
                item["rank"] = rank
            random.shuffle(self._items)
            logging.info(f"RSS: Fetched {len(self._items)} items from {url}")
        except Exception as e:
            logging.error(f"RSS: Failed to fetch {url}: {e}")

    def news_item(self):
        self.fetch()
        if not self._items:
            return None

        # Rotate, so repeated calls cycle through the feed
        # instead of consuming it and then running dry
        item = self._items.pop(0)
        self._items.append(item)

        return item


class OTD:

    def __init__(self, sources):
        self.events = []
        self.fetched_on = None

    # Lazy and dated, so constructing this never blocks on the network and
    # callers get the current day's events rather than those of the day
    # the process started. A failed refetch keeps the previous day's
    # events rather than nothing
    def fetch(self):
        import datetime
        today = datetime.date.today()
        if self.fetched_on == today and self.events:
            return

        url = ("https://en.wikipedia.org/api/rest_v1/feed/onthisday/events/"
               f"{today.month}/{today.day}")
        try:
            response = requests.get(url, timeout=10,
                                    headers={"User-Agent": USER_AGENT})
            if response.status_code == 200:
                events = response.json().get("events", [])
                if events:
                    self.events = events
                    self.fetched_on = today
                logging.info(f"otd: Fetched {len(events)} events for "
                             f"{today.month}/{today.day}")
            else:
                logging.error("Failed to fetch otd data: "
                              f"HTTP {response.status_code}")
        except Exception as e:
            logging.error(f"Failed to fetch otd data: {e}")

    def otd_item(self):
        self.fetch()
        if not self.events:
            return None

        # Rotate, so repeated calls cycle through the day's events
        # instead of showing the first one for ever
        event = self.events.pop(0)
        self.events.append(event)

        pages = event.get("pages") or []
        url = (pages[0].get("content_urls", {})
                       .get("desktop", {})
                       .get("page", "")) if pages else ""

        return {"year": event.get("year"),
                "text": event.get("text"),
                "url": url}


class Bluesky:
    '''
    Recent posts from a public Bluesky account through the unauthenticated
    AppView, so no token or login is needed

    Each dict carries the feed / title / url keys the other news sources use
    plus handle, text, created_at, link (an external url the post points to)
    and, when the post has a picture, image (a local file path) with
    image_url and image_alt
    '''

    api = "https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed"
    # posts are refetched only once they are this stale, so a display can
    # poll every few seconds
    refetch_after_s = 600

    def __init__(self, actor, limit=30, include_reposts=False, store=None):
        # actor is a handle, a did or a profile url
        self.actor = actor.rstrip("/").split("/profile/")[-1]
        self.limit = limit
        self.include_reposts = include_reposts
        self.store = store or cache
        self._items = []
        self._fetched_at = None
        self._lock = threading.Lock()

    def item_count(self):
        return len(self._items)

    # Lazy so construction never blocks on the network, and a failed refetch
    # keeps the posts already held
    def fetch(self):
        with self._lock:
            if (self._items and self._fetched_at is not None
                    and time.monotonic() - self._fetched_at
                    < self.refetch_after_s):
                return
            self._parse()

    def _parse(self):
        params = {"actor": self.actor, "limit": self.limit,
                  "filter": "posts_no_replies"}
        try:
            resp = requests.get(self.api, params=params, timeout=10,
                                headers={"User-Agent": USER_AGENT})
            resp.raise_for_status()
            feed = resp.json().get("feed") or []
        except Exception as e:
            logging.error(f"bluesky: failed to fetch {self.actor}: {e}")
            return

        items = []
        for entry in feed:
            reason = (entry.get("reason") or {}).get("$type", "")
            if reason.endswith("#reasonRepost") and not self.include_reposts:
                continue

            post = entry.get("post") or {}
            record = post.get("record") or {}
            text = (record.get("text") or "").strip()
            if not text:
                continue

            author = post.get("author") or {}
            handle = author.get("handle") or self.actor
            rkey = (post.get("uri") or "").rsplit("/", 1)[-1]
            link, image_url, image_alt = self._embed(post.get("embed") or {})
            items.append({
                "feed": author.get("displayName") or handle,
                "handle": handle,
                "title": text,
                "text": text,
                "url": f"https://bsky.app/profile/{handle}/post/{rkey}"
                       if rkey else "",
                "link": link,
                "image_url": image_url,
                "image_alt": image_alt,
                "created_at": record.get("createdAt") or "",
            })

        if items:
            for rank, item in enumerate(items, start=1):
                item["rank"] = rank
            self._items = items
            self._fetched_at = time.monotonic()

        logging.info(f"bluesky: fetched {len(items)} posts from {self.actor}")

    @staticmethod
    def _embed(view):
        '''
        The (link, image_url, image_alt) a post carries, read from the
        hydrated embed view where getAuthorFeed puts the cdn urls
        '''
        kind = view.get("$type", "")

        if kind == "app.bsky.embed.recordWithMedia#view":
            return Bluesky._embed(view.get("media") or {})

        if kind == "app.bsky.embed.images#view":
            first = (view.get("images") or [{}])[0]
            return ("", first.get("fullsize") or first.get("thumb") or "",
                    first.get("alt") or "")

        if kind == "app.bsky.embed.external#view":
            ext = view.get("external") or {}
            return (ext.get("uri") or "", ext.get("thumb") or "",
                    ext.get("title") or "")

        if kind == "app.bsky.embed.video#view":
            return "", view.get("thumbnail") or "", ""

        return "", "", ""

    # cdn.bsky.app negotiates to webp, which a Pillow built without libwebp
    # cannot open, so the picture silently drops from the view. The @jpeg
    # suffix pins the format the cdn returns
    @staticmethod
    def _pin_jpeg(url):
        if url.startswith("https://cdn.bsky.app/img/") and "@" not in url:
            return url + "@jpeg"
        return url

    def post_item(self):
        self.fetch()
        if not self._items:
            return None

        # rotate so repeated calls cycle through the posts
        item = self._items.pop(0)
        self._items.append(item)
        # picture resolved here, not during parse, so only shown posts fetch
        item["image"] = cache_image(self._pin_jpeg(item.get("image_url", "")),
                                    self.store)
        return item


class PrometheusClient:
    '''
    Reads named samples from a Prometheus /metrics endpoint. Stateless: every
    call fetches, so a view on a timer sees the current numbers
    '''

    def __init__(self, url, timeout=10):
        self.url = url if "://" in url else "http://" + url
        self.timeout = timeout

    # (name, series, value) for every sample line, series being the name with
    # any labels as the endpoint wrote them. The value is always after the
    # last '}' (or the name), so a label value with spaces does not fool it
    @staticmethod
    def _parse(text):
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line[0] == "#":
                continue
            if "{" in line:
                close = line.rfind("}")
                if close < 0:
                    continue
                name = line[:line.index("{")]
                series = line[:close + 1]
                fields = line[close + 1:].split()
            else:
                fields = line.split()
                name = series = fields.pop(0)
            if not fields:
                continue
            try:
                yield name, series, float(fields[0])
            except ValueError:
                continue

    # {series: value} for the wanted metric names, e.g.
    # 'iss_display_http_requests_total{path="/metrics"}'. No names asks for
    # everything the endpoint exposes
    def values(self, *names):
        try:
            resp = requests.get(self.url, timeout=self.timeout,
                                headers={"User-Agent": USER_AGENT})
            resp.raise_for_status()
        except requests.RequestException as e:
            logging.error(f"prometheus: {self.url}: {e}")

            return {}

        wanted = set(names)

        return {series: value
                for name, series, value in self._parse(resp.text)
                if not wanted or name in wanted}

    # One number for a metric, summed over its label series the way a
    # counter's total reads, or None when the endpoint has no such metric
    def value(self, name):
        series = self.values(name)

        return sum(series.values()) if series else None


class Py3status:

    def __init__(self, module_name):
        self.module_name = module_name
        self.config_common = """
general {
    colors = false
    interval = 5
    color_good = "#96b5b4"
}
"""
        self.module_config = {"mpd" : ""}
        self.module_config["mpd"] = self.config_common + """
order = "mpd"

"""
        self.module_config["net_iplist"] = self.config_common + """
order = "net_iplist"

net_iplist {
    iface_blacklist = ['lo0']
    ip_blacklist = ['127.*', '::1']
    format = "{format_iface}"
}
"""
        self.module_config["sysdata"] = self.config_common + r"""
order = "sysdata"

sysdata {
    format = "CPU Histogram [\?color=cpu_used_percent {format_cpu}]"
    format_cpu = "[\?if=used_percent>80 ⡇|[\?if=used_percent>60 ⡆"
    format_cpu += "|[\?if=used_percent>40 ⡄|[\?if=used_percent>20 ⡀"
    format_cpu += "|⠀]]]]"
    format_cpu_separator = ""
    thresholds = [(0, "good"), (60, "degraded"), (80, "bad")]
    cache_timeout = 1
}
"""
        self.module_config["online_status"] = self.config_common + """
order = "online_status"
"""
        self.module_config["uptime"] = self.config_common + r"""
order = "uptime"

uptime {
    format = 'up [\?if=weeks {weeks} weeks ][\?if=days {days} days ]'
    format += '[\?if=hours {hours} hours ][\?if=minutes {minutes} minutes ]'
}
"""
        self.module_config["whatismyip"] = self.config_common + """
order = "whatismyip"

whatismyip {
        format = '{icon} {ip} {country} {city}'
}
"""
        self.config_path = self.write_config()
        self.output = {}  # Store latest output per module

    def run_module(self):
        try:
            # -b routes notifications to notify-send instead of a nagbar,
            # so a failing module ends up in the log instead of spawning
            # a nagbar process
            cmd = ['py3status', '-c', self.config_path, '-o', '-b']
            exists = os.path.exists(self.config_path)
            readable = os.access(self.config_path, os.R_OK)

            logging.info(f"py3status config path: {self.config_path}, exists={exists}, readable={readable}")

            if readable:
                try:
                    with open(self.config_path, 'r', encoding='utf8') as cf:
                        logging.info(f"py3status config:\n{cf.read()}")
                except Exception as e:
                    logging.warning(f"py3status: failed reading config content: {e}")

            # Inherit current env PATH, optionally prepend /venv/bin if present
            env_run = os.environ.copy()
            venv_bin = '/venv/bin'
            if os.path.isdir(venv_bin):
                env_run['PATH'] = venv_bin + (os.pathsep + env_run['PATH'] if 'PATH' in env_run else '')

            p = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding='utf8',
                env=env_run
            )
            stdout, stderr = p.communicate()
            logging.info(f"py3status ({self.module_name}):\n{stdout}")

            if p.returncode != 0:
                logging.warning(f"py3status ({self.module_name}) exited with "
                                f"{p.returncode}: {(stderr or '').strip()}")
            elif stderr:
                logging.debug(f"py3status stderr ({self.module_name}): {stderr.strip()}")

            data = None
            if stdout:
                lines = [l.strip() for l in stdout.splitlines() if l.strip()]
                tail = []
                for i in range(len(lines) - 1, -1, -1):
                    tail.insert(0, lines[i])
                    try:
                        data = json.loads("\n".join(tail))
                        break
                    except Exception:
                        continue

            # Extract full_text
            result = data
            try:
                if isinstance(data, dict) and 'full_text' in data:
                    result = data['full_text']
                elif isinstance(data, list):
                    blocks = data[-1] if (data and isinstance(data[-1], list)) else data
                    if isinstance(blocks, list):
                        for blk in reversed(blocks):
                            if isinstance(blk, dict) and 'full_text' in blk:
                                result = blk['full_text']
                                break
            except Exception:
                pass

            # Fallback: regex the last full_text from raw stdout
            if result is None:
                import re
                matches = re.findall(r'"full_text"\s*:\s*"([^"]+)"', stdout or '')
                if matches:
                    result = matches[-1]

            # Decode any unicode escape sequences (e.g., \\u25cf) to actual characters
            if isinstance(result, str):
                try:
                    result = bytes(result, "utf-8").decode("unicode_escape")
                except Exception:
                    pass

            # A module that failed renders as its own name, which is not
            # data for anyone downstream
            if result == self.module_name:
                logging.warning(f"py3status ({self.module_name}): "
                                "module failed, returning no data")
                return None

            logging.info(f"py3status ({self.module_name}) parsed: {result}")
            return result
        except FileNotFoundError:
            # py3status is optional, so a missing binary means no data, like
            # any other unreachable source
            if "py3status" not in _missing_tools_seen:
                _missing_tools_seen.add("py3status")
                logging.warning("py3status is not installed, dependent "
                                "modules return no data")
            return None
        except OSError as e:
            logging.warning(f"py3status ({self.module_name}): {e}")
            return None
        finally:
            try:
                os.remove(self.config_path)
            except Exception:
                pass

    def write_config(self):
        tmp = tempfile.NamedTemporaryFile(delete=False, mode='w+', encoding='utf-8')
        # Always write only the config for this module
        tmp.write(self.module_config[self.module_name])
        tmp.flush()
        tmp.close()
        return tmp.name


# ps and uptime are absent from some minimal images by design, and the /proc
# fallbacks are complete, so a missing tool is a permanent state not an error
_missing_tools_seen = set()


def _note_missing_tool(tool):
    if tool not in _missing_tools_seen:
        _missing_tools_seen.add(tool)
        logging.debug("%s not found, reading /proc directly", tool)


class System:

    @staticmethod
    def process_list():
        '''
        Every process as its process_cpu_times row plus the command line,
        by pid. A kernel thread has no command line and shows its name in
        brackets, the way ps does
        '''
        procs = System.process_cpu_times()
        for proc in procs:
            try:
                with open(f"/proc/{proc['pid']}/cmdline", encoding="utf-8") as f:
                    cmdline = f.read().replace("\x00", " ").strip()
            except OSError:
                cmdline = ""
            proc["cmdline"] = cmdline or f"[{proc['name']}]"
        procs.sort(key=lambda p: p["pid"])

        return procs

    def os_release():
        data = ""
        try:
            with open('/etc/os-release', encoding='utf-8') as f:
                for line in skip_comments(f):
                    if line.startswith("PRETTY_NAME"):
                        data = line.split("=", 1)[1].strip().strip('"')
                        break
        except FileNotFoundError:
            logging.warning("/etc/os-release not found, falling back to uname")
            data = f"{platform.system()} {platform.release()}"
        except Exception as e:
            logging.error(f"Failed reading /etc/os-release: {e}")
        return data

    def sysdata():
        '''The py3status sysdata line, cpu and memory as that module formats it'''
        return Py3status("sysdata").run_module()

    @staticmethod
    def net_addresses():
        '''
        The machine's own addresses as rows of dev and ip, without the loopback,
        read from the kernel rather than from a shelled-out tool
        '''
        import array
        import fcntl

        rows = []

        # IPv4 over SIOCGIFCONF: the kernel fills ifreq structs of name
        # and address, 40 bytes each on 64 bit
        try:
            buffer = array.array('B', b'\0' * 4096)
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                filled = struct.unpack(
                    'iL', fcntl.ioctl(
                        s.fileno(), 0x8912,
                        struct.pack('iL', len(buffer),
                                    buffer.buffer_info()[0])))[0]
            data = buffer.tobytes()
            for n in range(0, filled, 40):
                name = data[n:n + 16].split(b'\0', 1)[0].decode()
                address = socket.inet_ntoa(data[n + 20:n + 24])
                if name != "lo" and not address.startswith("127."):
                    rows.append({"dev": name, "ip": address})
        except (OSError, ValueError) as e:
            logging.warning(f"net: Failed to list IPv4 addresses: {e}")

        try:
            with open('/proc/net/if_inet6', encoding='utf-8') as f:
                for line in f:
                    fields = line.split()
                    if len(fields) < 6:
                        continue
                    address = socket.inet_ntop(socket.AF_INET6,
                                               bytes.fromhex(fields[0]))
                    name = fields[5]
                    if name != "lo" and address != "::1":
                        rows.append({"dev": name, "ip": address})
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            logging.warning(f"net: Failed to list IPv6 addresses: {e}")

        return rows

    def net_data(probe_ip_address):
        net = {"address" : None,
               "addresses" : "",
               "public_ip" : "",
               "online_status" : "",
               "resolvconf" : ""}
        net["address"] = System.net_iface_address(probe_ip_address)
        net["addresses"] = System.net_addresses()
        whatismyip = Py3status("whatismyip")
        net["public_ip"] = whatismyip.run_module()
        online_status = Py3status("online_status")
        net["online_status"] = online_status.run_module()
        net["resolvconf"] = System.net_resolvconf()

        return net

    def net_resolvconf():
        data = ""
        try:
            with open('/etc/resolv.conf', encoding='utf-8') as f:
                for line in skip_comments(f):
                    data += line + "\n"
        except FileNotFoundError:
            logging.warning("/etc/resolv.conf not found")
        except Exception as e:
            logging.error(f"Failed reading /etc/resolv.conf: {e}")
        return data

    # The kernel's own high water mark of a process's resident set, so a
    # spike between two samples is not lost. 0 for a kernel thread
    @staticmethod
    def rss_peak(pid):
        try:
            with open(f"/proc/{pid}/status", encoding='utf-8') as f:
                for line in f:
                    if line.startswith("VmHWM:"):
                        return int(line.split()[1]) * 1024
        except (OSError, ValueError, IndexError):
            pass

        return 0

    @staticmethod
    def process_cpu_times():
        ticks = os.sysconf('SC_CLK_TCK')
        page_size = os.sysconf('SC_PAGE_SIZE')
        procs = []
        for entry in os.listdir('/proc'):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/stat", encoding='utf-8') as f:
                    data = f.read()
            except OSError:
                continue

            # comm is parenthesised and may itself contain spaces or parens,
            # so the last closing paren ends it and the numbers follow
            start, end = data.find('('), data.rfind(')')
            if start < 0 or end < 0:
                continue

            fields = data[end + 2:].split()
            try:
                cpu_s = (int(fields[11]) + int(fields[12])) / ticks
                threads = int(fields[17])
                starttime = int(fields[19])
                rss_b = int(fields[21]) * page_size
            except (IndexError, ValueError, ZeroDivisionError):
                continue

            procs.append({"pid": int(entry),
                          "name": data[start + 1:end],
                          "cpu_s": cpu_s,
                          "threads": threads,
                          "starttime": starttime,
                          "rss_b": rss_b,
                          "rss_peak_b": System.rss_peak(entry)})

        return procs

    @staticmethod
    def memory():
        '''Total, available and swap in bytes from /proc/meminfo, or None'''
        wanted = {"MemTotal": "total_b", "MemAvailable": "available_b",
                  "SwapTotal": "swap_total_b", "SwapFree": "swap_free_b"}
        fields = {}
        try:
            with open("/proc/meminfo", encoding="utf-8") as f:
                for line in f:
                    name, _, rest = line.partition(":")
                    if name in wanted and rest.split():
                        fields[wanted[name]] = int(rest.split()[0]) * 1024
        except (OSError, ValueError):
            return None

        return fields if fields.get("total_b") else None

    # One walk answers both the biggest files and the biggest directories,
    # so both callers share it rather than each paying for a traversal
    _sizes_cache = {}
    _sizes_lock = threading.Lock()

    @staticmethod
    def host_uptime_seconds():
        """
        Seconds since the host booted, or None where /proc/uptime is absent.

        In a container this is the uptime of the kernel the container runs on,
        which is the host or the vm hosting it, not the container's own.
        """
        try:
            with open("/proc/uptime", encoding="utf-8") as f:
                return float(f.read().split()[0])
        except (OSError, ValueError, IndexError) as e:
            logging.debug(f"uptime: Cannot read /proc/uptime: {e}")

            return None

    @staticmethod
    def load_average():
        '''The 1, 5 and 15 minute load from /proc/loadavg, or None'''
        try:
            with open('/proc/loadavg', encoding='utf-8') as f:
                return [float(v) for v in f.read().split()[:3]]
        except (OSError, ValueError) as e:
            logging.debug(f"uptime: Cannot read /proc/loadavg: {e}")

            return None

    @staticmethod
    def scan_sizes(path="/", limit=12, budget_s=20.0, max_entries=2_000_000):
        """
        Walks path once and returns the largest files and directories under it.

        Sizes are blocks actually used rather than apparent size, which is what
        fills a disk and what du reports. The walk stays on the one filesystem
        it started on, so it never wanders into /proc, /sys or /dev, and it
        follows no symlinks, so nothing is counted twice and nothing loops.
        """
        with System._sizes_lock:
            cached = System._sizes_cache.get((path, limit))
        if cached:
            return cached

        started = time.time()
        try:
            root_dev = os.stat(path).st_dev
        except OSError as e:
            logging.error(f"scan_sizes: Cannot stat {path}: {e}")

            return {"files": [], "dirs": [], "truncated": False, "path": path}

        files = []
        # Every directory on the way up gets the size of what is below it
        totals = {}
        seen = 0
        truncated = False
        stack = [path]
        while stack:
            if time.time() - started > budget_s or seen > max_entries:
                truncated = True
                break

            current = stack.pop()
            try:
                entries = list(os.scandir(current))
            except OSError:
                continue

            for entry in entries:
                seen += 1
                try:
                    if entry.is_symlink():
                        continue
                    st = entry.stat(follow_symlinks=False)
                    if st.st_dev != root_dev:
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
                        continue
                except OSError:
                    continue

                size = st.st_blocks * 512
                files.append((size, entry.path))
                parent = current
                while True:
                    totals[parent] = totals.get(parent, 0) + size
                    if parent == path or parent in ("/", ""):
                        break
                    parent = os.path.dirname(parent)

        files.sort(reverse=True)
        dirs = sorted(((v, k) for k, v in totals.items()), reverse=True)
        result = {"files": files[:limit], "dirs": dirs[:limit],
                  "truncated": truncated, "path": path,
                  "took_s": time.time() - started}
        with System._sizes_lock:
            System._sizes_cache[(path, limit)] = result

        return result

    @staticmethod
    def icmp_checksum(data):
        if len(data) % 2:
            data += b"\x00"

        total = 0
        for i in range(0, len(data), 2):
            total += (data[i] << 8) + data[i + 1]
        while total >> 16:
            total = (total & 0xffff) + (total >> 16)

        return ~total & 0xffff

    @staticmethod
    def icmp_echo(ident, seq):
        header = struct.pack("!BBHHH", 8, 0, 0, ident, seq)
        payload = b"iss-display"
        checksum = System.icmp_checksum(header + payload)

        return struct.pack("!BBHHH", 8, 0, checksum, ident, seq) + payload

    @staticmethod
    def icmp_reply(packet, ident, seq):
        ihl = (packet[0] & 0x0f) * 4
        icmp = packet[ihl:]
        if len(icmp) < 8:
            return None

        kind, _, _, got_id, got_seq = struct.unpack("!BBHHH", icmp[:8])
        if kind == 0:
            return "reached" if (got_id, got_seq) == (ident, seq) else None

        if kind not in (3, 11) or len(icmp) < 16:
            return None

        inner = icmp[8:]
        inner_icmp = inner[(inner[0] & 0x0f) * 4:]
        if len(inner_icmp) < 8:
            return None

        got_id, got_seq = struct.unpack("!HH", inner_icmp[4:8])
        if (got_id, got_seq) != (ident, seq):
            return None

        return "unreachable" if kind == 3 else "hop"

    @staticmethod
    def traceroute_probe(sock, dest, ttl, ident, seq, wait_s):
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_TTL, ttl)
        sent = time.monotonic()
        try:
            sock.sendto(System.icmp_echo(ident, seq), (dest, 0))
        except OSError as e:
            logging.warning(f"traceroute: send to {dest} failed: {e}")
            return None, None, None

        deadline = sent + wait_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None, None, None

            if not select.select([sock], [], [], remaining)[0]:
                return None, None, None

            packet, addr = sock.recvfrom(1024)
            kind = System.icmp_reply(packet, ident, seq)
            if kind:
                return addr[0], (time.monotonic() - sent) * 1000, kind

    @staticmethod
    def traceroute(target, max_hops=20, wait_s=1, cycles=5, timeout_s=120):
        '''
        The route to target: dest, cycles, the hops by ttl with the hosts
        that answered, the probes sent and the round trip times in ms,
        stopped when the time budget ran out. Only error is set when the
        trace could not run at all
        '''
        trace = {"target": target, "dest": None, "cycles": cycles,
                 "hops": [], "stopped": False, "error": None}
        if not target:
            trace["error"] = "no target"
            return trace

        try:
            dest = socket.gethostbyname(target)
        except OSError as e:
            logging.warning(f"traceroute: cannot resolve {target}: {e}")
            trace["error"] = f"cannot resolve {target}"
            return trace
        trace["dest"] = dest

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_RAW,
                                 socket.IPPROTO_ICMP)
        except PermissionError:
            logging.warning("traceroute: raw sockets need CAP_NET_RAW")
            trace["error"] = "raw sockets need CAP_NET_RAW"
            return trace
        except OSError as e:
            logging.warning(f"traceroute: cannot open a raw socket: {e}")
            trace["error"] = "cannot open a raw socket"
            return trace

        ident = os.getpid() & 0xffff
        deadline = time.monotonic() + timeout_s
        hops = {}
        last_ttl = max_hops
        stopped = False

        with sock:
            for cycle in range(cycles):
                ttl = 1
                while ttl <= last_ttl:
                    if time.monotonic() > deadline:
                        stopped = True
                        break

                    hop = hops.setdefault(ttl, {"hosts": [], "sent": 0,
                                                "times": []})
                    hop["sent"] += 1
                    host, rtt, kind = System.traceroute_probe(
                        sock, dest, ttl, ident, (cycle * 256 + ttl) & 0xffff,
                        wait_s)
                    if host is not None:
                        if host not in hop["hosts"]:
                            hop["hosts"].append(host)
                        hop["times"].append(rtt)
                        if kind == "reached":
                            last_ttl = ttl
                    ttl += 1

                if stopped:
                    break

        trace["hops"] = [{"ttl": ttl, **hops[ttl]} for ttl in sorted(hops)]
        trace["stopped"] = stopped

        return trace

    tcp_states = {"01": "ESTABLISHED",
                  "02": "SYN_SENT",
                  "03": "SYN_RECV",
                  "04": "FIN_WAIT1",
                  "05": "FIN_WAIT2",
                  "06": "TIME_WAIT",
                  "07": "CLOSE",
                  "08": "CLOSE_WAIT",
                  "09": "LAST_ACK",
                  "0A": "LISTEN",
                  "0B": "CLOSING"}

    @staticmethod
    def net_hex_address(hex_address):
        if len(hex_address) == 8:
            return socket.inet_ntop(socket.AF_INET,
                                    bytes.fromhex(hex_address)[::-1])
        if len(hex_address) == 32:
            packed = b"".join(bytes.fromhex(hex_address[i:i + 8])[::-1]
                              for i in range(0, 32, 8))
            return socket.inet_ntop(socket.AF_INET6, packed)

        return hex_address

    @staticmethod
    def net_endpoint(field):
        address, _, port = field.rpartition(':')
        try:
            port = int(port, 16)
        except ValueError:
            return field

        return f"[{System.net_hex_address(address)}]:{port}" \
            if len(address) == 32 else f"{System.net_hex_address(address)}:{port}"

    netlink_sock_diag = 4
    sock_diag_by_family = 20
    inet_diag_info = 2
    # Offsets into struct tcp_info, checked against ss -ti
    tcpi_bytes_received = 128
    tcpi_bytes_sent = 200

    @staticmethod
    def tcp_diag_request(family):
        # struct inet_diag_req_v2, then an empty inet_diag_sockid
        req = struct.pack("=BBBBI", family, socket.IPPROTO_TCP,
                          1 << (System.inet_diag_info - 1), 0, 0xfff)
        req += b"\0" * 48

        return struct.pack("=IHHII", 16 + len(req), System.sock_diag_by_family,
                           0x301, 1, os.getpid()) + req

    @staticmethod
    def tcp_diag_row(body, family):
        if len(body) < 72:
            return None

        sport, dport = struct.unpack("!HH", body[4:8])
        local = System.net_diag_endpoint(body[8:24], sport, family)
        remote = System.net_diag_endpoint(body[24:40], dport, family)
        sent = received = None
        off = 72
        while off + 4 <= len(body):
            alen, atype = struct.unpack("=HH", body[off:off + 4])
            if alen < 4:
                break
            data = body[off + 4:off + alen]
            if (atype == System.inet_diag_info
                    and len(data) >= System.tcpi_bytes_sent + 8):
                received = struct.unpack_from("=Q", data,
                                              System.tcpi_bytes_received)[0]
                sent = struct.unpack_from("=Q", data,
                                          System.tcpi_bytes_sent)[0]
            off += (alen + 3) & ~3

        return (local, remote), sent, received

    @staticmethod
    def net_diag_endpoint(raw, port, family):
        if family == socket.AF_INET:
            return f"{socket.inet_ntop(family, raw[:4])}:{port}"

        return f"[{socket.inet_ntop(family, raw[:16])}]:{port}"

    @staticmethod
    def tcp_byte_counts():
        """
        Bytes sent and received per tcp socket, from tcp_info over netlink.

        /proc/net/tcp carries queue depths, not totals, so the counters have
        to come from sock_diag. Anything that goes wrong here leaves the
        table without the columns rather than without the sockets.
        """
        counts = {}
        for family in (socket.AF_INET, socket.AF_INET6):
            try:
                s = socket.socket(socket.AF_NETLINK, socket.SOCK_DGRAM,
                                  System.netlink_sock_diag)
            except OSError as e:
                logging.info(f"sockets: No netlink sock_diag: {e}")

                return counts

            try:
                with s:
                    s.settimeout(2)
                    s.send(System.tcp_diag_request(family))
                    while True:
                        buf = s.recv(65536)
                        off, done = 0, False
                        while off + 16 <= len(buf):
                            length, mtype = struct.unpack("=IH", buf[off:off + 6])
                            if length < 16 or mtype in (2, 3):
                                done = True
                                break
                            row = System.tcp_diag_row(
                                buf[off + 16:off + length], family)
                            if row and row[1] is not None:
                                counts[row[0]] = (row[1], row[2])
                            off += (length + 3) & ~3
                        if done:
                            break
            except (OSError, socket.timeout, struct.error) as e:
                logging.info(f"sockets: sock_diag dump failed: {e}")

        return counts

    @staticmethod
    def net_socket_list():
        counts = System.tcp_byte_counts()
        sockets = []
        for proto, path in (("tcp", "/proc/net/tcp"), ("tcp", "/proc/net/tcp6"),
                            ("udp", "/proc/net/udp"), ("udp", "/proc/net/udp6")):
            try:
                with open(path, encoding='utf-8') as f:
                    next(f, None)
                    for line in f:
                        fields = line.split()
                        if len(fields) < 4:
                            continue
                        local = System.net_endpoint(fields[1])
                        remote = System.net_endpoint(fields[2])
                        if proto == "udp":
                            state = "UNCONN" if remote.endswith(":0") else "ESTABLISHED"
                        else:
                            state = System.tcp_states.get(fields[3].upper(), fields[3])
                        sent, received = counts.get((local, remote),
                                                   (None, None))
                        sockets.append({"proto": proto,
                                        "local": local,
                                        "remote": remote,
                                        "state": state,
                                        "sent": sent,
                                        "received": received})
            except FileNotFoundError:
                continue
            except Exception as e:
                logging.error(f"Failed reading {path}: {e}")

        return sockets

    # linux/neighbour.h: struct ndmsg is family/pad1 u8, pad2 u16,
    # ifindex s32, state u16, flags u8, type u8
    _NDA_DST = 1
    _NDA_LLADDR = 2
    _NUD = {0x01: "INCOMPLETE", 0x02: "REACHABLE", 0x04: "STALE",
            0x08: "DELAY", 0x10: "PROBE", 0x20: "FAILED",
            0x40: "NOARP", 0x80: "PERMANENT"}

    @staticmethod
    def _arp_from_proc():
        # ipv4 only, no ndp, and the state is just complete or not, but it
        # needs neither netlink nor a capability
        rows = []
        try:
            with open("/proc/net/arp", encoding="utf-8") as f:
                next(f, None)
                for line in f:
                    parts = line.split()
                    if len(parts) < 6:
                        continue
                    ip, _, flags, mac, _, dev = parts[:6]
                    rows.append({
                        "ip": ip,
                        "mac": "" if mac == "00:00:00:00:00:00" else mac,
                        "dev": dev,
                        "state": "REACHABLE" if flags == "0x2" else "INCOMPLETE",
                    })
        except OSError:
            pass

        return rows

    @staticmethod
    def _neigh_row(body, ifnames):
        if len(body) < 12:
            return None
        fam, _, ifindex, state, _, _ = struct.unpack("=BxHiHBB", body[:12])
        if fam not in (socket.AF_INET, socket.AF_INET6):
            return None

        ip = mac = ""
        off = 12
        while off + 4 <= len(body):
            alen, atype = struct.unpack("=HH", body[off:off + 4])
            if alen < 4:
                break
            val = body[off + 4:off + alen]
            if atype == System._NDA_DST:
                n = 16 if fam == socket.AF_INET6 else 4
                try:
                    ip = socket.inet_ntop(fam, val[:n])
                except (OSError, ValueError):
                    pass
            elif atype == System._NDA_LLADDR and len(val) == 6:
                mac = ":".join(f"{b:02x}" for b in val)
            off += (alen + 3) & ~3

        if not ip:
            return None

        return {"ip": ip, "mac": mac, "dev": ifnames.get(ifindex, str(ifindex)),
                "state": System._NUD.get(state, str(state))}

    @staticmethod
    def net_neighbour_list():
        """
        The kernel neighbour table -- arp for ipv4, ndp for ipv6 -- read the
        way `ip neigh show` reads it, over RTM_GETNEIGH on NETLINK_ROUTE,
        since the image has no iproute2. Each entry: ip, mac, dev, state.
        Falls back to /proc/net/arp (ipv4 only) when netlink is unavailable.
        """
        RTM_GETNEIGH, RTM_NEWNEIGH = 30, 28
        try:
            ifnames = dict(socket.if_nameindex())
        except OSError:
            ifnames = {}

        try:
            s = socket.socket(socket.AF_NETLINK, socket.SOCK_DGRAM, 0)
        except (OSError, AttributeError) as e:
            logging.info(f"neighbours: no netlink route: {e}")

            return System._arp_from_proc()

        entries = []
        try:
            with s:
                s.settimeout(2)
                ndmsg = struct.pack("=BxHiHBB", socket.AF_UNSPEC, 0, 0, 0, 0, 0)
                s.send(struct.pack("=IHHII", 16 + len(ndmsg), RTM_GETNEIGH,
                                   0x301, 1, os.getpid()) + ndmsg)
                done = False
                while not done:
                    buf = s.recv(65536)
                    off = 0
                    while off + 16 <= len(buf):
                        mlen, mtype = struct.unpack("=IH", buf[off:off + 6])
                        if mlen < 16 or mtype in (2, 3):
                            done = True
                            break
                        if mtype == RTM_NEWNEIGH:
                            row = System._neigh_row(buf[off + 16:off + mlen],
                                                    ifnames)
                            if row:
                                entries.append(row)
                        off += (mlen + 3) & ~3
        except (OSError, socket.timeout, struct.error) as e:
            logging.info(f"neighbours: netlink dump failed: {e}")

            return System._arp_from_proc()

        return entries or System._arp_from_proc()

    @staticmethod
    def net_valid_ip_address(ip_address):
        # An unset target is expected and not an error
        if not ip_address:
            return False
        try:
            ipaddress.ip_address(ip_address)
            return True
        except ValueError:
            logging.warning(f"{ip_address} is an invalid IP address")
            return False

    def net_iface_address(target):
        # target is an IP or a hostname. The local address facing it is the
        # source address the kernel assigns to a UDP socket connected toward
        # it. getaddrinfo resolves the name and settles the family. Nothing
        # is sent
        if not target:
            return ""
        try:
            family, _, _, _, sockaddr = socket.getaddrinfo(
                target, 80, type=socket.SOCK_DGRAM)[0]
        except socket.gaierror as e:
            logging.warning(f"net: cannot resolve probe target {target!r}: {e}")
            return ""

        try:
            with socket.socket(family, socket.SOCK_DGRAM) as s:
                s.settimeout(0.5)
                try:
                    s.connect(sockaddr)
                except OSError as e:
                    logging.info(f"{target} is unreachable or no route: {e}")
                    return ""
                return s.getsockname()[0]
        except Exception as e:
            logging.error(f"Failed to determine local address for {target}: {e}")
            return ""



class Weather:

    def __init__(self, location):
        self.location = location
        self.weather = self.fetch_weather()

    def fetch_weather(self):
        url = f"https://wttr.in/{self.location}?format=j1"
        logging.info(f"iss-weather: Fetching weather for {self.location} at {url}")
        try:
            response = requests.get(url, timeout=10)
            if response and response.content:
                return json.loads(response.content)
            logging.error("Failed to fetch weather data: empty response")
        except requests.ReadTimeout as e:
            logging.error(f"weather: Timeout for request {e}")
        except ValueError:
            logging.error("weather: Failed to decode data")
        except Exception as e:
            logging.error(f"weather: Error requesting weather: {e}")

        return None

    def current(self):
        '''
        The current conditions as values, None where the source had nothing:
        temperature, condition and its wttr code, wind, area, sunrise and
        sunset, moon phase and days to full, uv index
        '''
        out = {"temp_c": None, "condition": None, "condition_code": None,
               "wind_kmh": None, "area": None, "sunrise": None, "sunset": None,
               "moon_phase": None, "days_to_full_moon": None, "uv_index": None}
        try:
            now = self.weather["current_condition"][0]
            out["temp_c"] = int(now["temp_C"])
            out["condition"] = now["weatherDesc"][0]["value"]
            out["condition_code"] = int(now.get("weatherCode") or 0) or None
            out["wind_kmh"] = int(now["windspeedKmph"])
            out["area"] = self.weather["nearest_area"][0]["areaName"][0]["value"]
        except (KeyError, IndexError, TypeError, ValueError) as e:
            if self.weather is not None:
                logging.error(f"weather: Unexpected data format: {e}")

        today = (Calendar.sunrise_sunset(location=self.location) or {}).get("today", {})
        out["sunrise"] = today.get("sunrise") or None
        out["sunset"] = today.get("sunset") or None
        out["moon_phase"], _, out["days_to_full_moon"] = \
            Calendar.moonphase(location=self.location)
        out["uv_index"] = Weather.fetch_uv_index(location=self.location)

        return out

    @staticmethod
    def fetch_uv_index(location="Berlin"):
        '''The uv index from wttr.in as a number, or None'''
        url = f"https://wttr.in/{location}?format=%u"
        try:
            response = requests.get(url, timeout=10)
            if response.status_code == 200:
                return float(response.text.strip())
            logging.error(f"Failed to fetch UV index: HTTP {response.status_code}")
        except (ValueError, TypeError):
            logging.error("Failed to read the UV index")
        except Exception as e:
            logging.error(f"Error fetching UV index: {e}")

        return None
