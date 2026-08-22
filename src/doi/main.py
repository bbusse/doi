import ipaddress
import logging
import json
import os
import platform
import random
import socket
import sqlite3
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import requests
from hnapi import HnApi
from paho.mqtt import client as mqtt_client


def skip_comments(file):
    for line in file:
        if not line.strip().startswith('#'):
            yield line.strip()


class APOD:

    def __init__(self, api_key="DEMO_KEY", save_dir="/tmp"):
        self.api_key = api_key
        self.save_dir = save_dir

    def apod_data(self):
        """
        Downloads NASA's Astronomy Picture of the Day (APOD) and returns its description text.
        Returns:
            tuple: (image_path, description_string) or (None, None) on failure.
        """
        apod_url = f"https://api.nasa.gov/planetary/apod?api_key={self.api_key}"
        try:
            resp = requests.get(apod_url, timeout=10)
            if resp.status_code != 200:
                logging.error(f"APOD: Failed to fetch metadata: {resp.status_code}")
                return None, None
            data = resp.json()
            img_url = data.get("hdurl") or data.get("url")
            desc = data.get("explanation", "")
            if not img_url:
                logging.error("APOD: No image URL found in response")
                return None, None

            # Download image
            img_resp = requests.get(img_url, timeout=10)
            if img_resp.status_code != 200:
                logging.error(f"APOD: Failed to download image: {img_resp.status_code}")
                return None, None

            img_ext = os.path.splitext(img_url)[-1]
            img_path = os.path.join(self.save_dir, f"apod{img_ext}")
            with open(img_path, "wb") as f:
                f.write(img_resp.content)

            # Return image path and description string
            return img_path, desc
        except Exception as e:
            logging.error(f"APOD: Error fetching APOD: {e}")
            return None, None


class Music:

    def __init__(self):
        self.music = {'mpd_data'   : False,
                      'mpd_state'  : False,
                      'mpd_artist' : "",
                      'mpd_title'  : "",
                      'mpd_album'  : ""}

    def mpd(self):
        mpd = Py3status("mpd")
        data = mpd.run_module()
        return data


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
        self.mqtt_views = list()

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
            self.active_msg = ""
            try:
                jm = msg.payload.decode(errors='ignore')
            except Exception:
                jm = ""

            # Try JSON parse, fall back to raw text
            try:
                m = json.loads(jm)
            except Exception:
                m = None

            logging.info(f"Received `{m if m is not None else jm}` in  `{msg.topic}`")
            texts = []

            topic = msg.topic or ""
            if isinstance(m, dict):
                if topic == "hyperblast/current_song":
                    title = m.get("title") or ""
                    file_name = m.get("file") or ""
                    if title:
                        texts.append(title)
                    if file_name:
                        texts.append(f"[{file_name}]")
                elif topic == "sensor/mainhallsensor/temperature":
                    texts.append("Mainhall")
                    val = m.get("value") if isinstance(m.get("value"), (int, float, str)) else m
                    texts.append(f"{val} °C")
                else:
                    # Generic dict payload
                    texts.append(json.dumps(m, ensure_ascii=False))
            elif m is not None:
                # JSON but not dict (e.g. list/number/string)
                texts.append(json.dumps(m, ensure_ascii=False))
            else:
                # Not JSON, raw text
                if jm:
                    texts.append(jm)

            if not texts:
                texts = [str(jm)]

            on_message_callback(topic, texts)
            self.mqtt_views.append(msg.topic)

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
        n = {"title": "",
             "url": ""}

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

    def hn_fetch_top_news(self, nitems):
        n = 0

        logging.info("Fetching News")
        con = HnApi()
        top = con.get_top()

        for tnews in top:
            if n == nitems:
                break

            self.news.append(con.get_item(tnews))
            n += 1


class RSSFeed:
    ATOM_NS = "http://www.w3.org/2005/Atom"

    def __init__(self, url):
        self._items = []
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
            random.shuffle(self._items)
            logging.info(f"RSS: Fetched {len(self._items)} items from {url}")
        except Exception as e:
            logging.error(f"RSS: Failed to fetch {url}: {e}")

    def news_item(self):
        return self._items.pop(0) if self._items else None


class OTD:

    def __init__(self, sources):
        import datetime
        today = datetime.date.today()
        month = today.month
        day = today.day
        url = f"https://en.wikipedia.org/api/rest_v1/feed/onthisday/events/{month}/{day}"
        self.events = []

        try:
            response = requests.get(url, timeout=10)
            if response.status_code == 200:
                data = response.json()
                for event in data.get("events", []):
                    logging.info(f"{event['year']}: {event['text']}")
                    self.events.append(event)
            else:
                logging.error(f"Failed to fetch otd data: HTTP {response.status_code}")
        except Exception as e:
            logging.error(f"Failed to fetch otd data: {e}")

    def otd_item(self):
        n = {"year": "",
             "text": ""}

        if not self.events:
            return None

        n["year"] = self.events[0].get('year')
        n["text"] = self.events[0].get('text')
        return n


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
        format = 'up [\?if=weeks {weeks} weeks ][\?if=days {days} days ]
        [\?if=hours {hours} hours ][\?if=minutes {minutes} minutes ]'
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
            cmd = ['py3status', '-c', self.config_path, '-o']
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

            if stderr:
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

            logging.info(f"py3status ({self.module_name}) parsed: {result}")
            return result
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


class System:

    def list_processes(limit=0):
        # Choose ps variant by platform
        system = platform.system()
        if system == 'Linux':
            # Processes without kernel threads
            cmd = ['ps', 'wux', '--ppid', '2', '-p', '2', '--deselect']
        elif system in ('Darwin', 'FreeBSD'):
            cmd = ['ps', 'aux']
        else:
            cmd = ['ps', 'aux']

        try:
            ps = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                shell=False,
                encoding="utf8"
            ).communicate()[0]
        except FileNotFoundError:
            logging.warning("ps not found, falling back to /proc")
            ps = System._processes_from_proc()
        except Exception as e:
            logging.error(f"ps failed: {e}")
            ps = ""
        if limit and ps:
            lines = ps.splitlines()
            return "\n".join(lines[:max(0, limit)])
        return ps

    def _processes_from_proc():
        try:
            entries = sorted(
                (e for e in os.scandir('/proc') if e.name.isdigit()),
                key=lambda e: int(e.name)
            )
        except OSError as e:
            return f"Process list unavailable: {e}"

        lines = ["PID    COMMAND"]
        for entry in entries:
            pid = entry.name
            try:
                with open(f'/proc/{pid}/cmdline', encoding='utf8') as f:
                    cmdline = f.read().replace('\x00', ' ').strip()
                if not cmdline:
                    with open(f'/proc/{pid}/status', encoding='utf8') as f:
                        for line in f:
                            if line.startswith('Name:'):
                                cmdline = '[' + line.split(':', 1)[1].strip() + ']'
                                break
                lines.append(f"{pid:<6} {cmdline}")
            except OSError:
                continue
        return "\n".join(lines)

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

    def sys_data():
        sys = {"data" : None,
               "uptime" : ""}
        sysdata = Py3status("sysdata")
        data = sysdata.run_module()
        sys["data"] = data
        uptime = Py3status("uptime")
        sys["uptime"] = uptime.run_module()

        return sys

    def net_data(probe_ip_address):
        net = {"address" : None,
               "addresses" : "",
               "public_ip" : "",
               "online_status" : "",
               "resolvconf" : ""}
        net["address"] = System.net_iface_address(probe_ip_address)
        net_iplist = Py3status("net_iplist")
        net["addresses"] = net_iplist.run_module()
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

    def net_valid_ip_address(ip_address):
        try:
            ipaddress.ip_address(ip_address)
            return True
        except ValueError:
            logging.warning(f"{ip_address} is an invalid IP address")
            return False

    def net_iface_address(ip_address):
        try:
            ip_obj = ipaddress.ip_address(ip_address)
        except ValueError:
            logging.warning(f"{ip_address} is an invalid IP address")
            return ""

        family = socket.AF_INET6 if ip_obj.version == 6 else socket.AF_INET
        try:
            with socket.socket(family, socket.SOCK_DGRAM) as s:
                s.settimeout(0.5)
                try:
                    s.connect((ip_address, 80))
                except OSError as e:
                    logging.info(f"{ip_address} is unreachable or no route: {e}")
                    return ""
                return s.getsockname()[0]
        except Exception as e:
            logging.error(f"Failed to determine local address for {ip_address}: {e}")
            return ""

    @staticmethod
    def uptime(env):
        import re

        try:
            p = subprocess.Popen(['uptime'],
                             stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT,
                             encoding="utf8",
                             env=env)
            output, error = p.communicate()
        except FileNotFoundError:
            logging.warning("uptime not found, falling back to /proc")
            return System._uptime_from_proc()

        # Example: ' 10:23:45 up 1 day,  2:34,  3 users,  load average: 0.00, 0.01, 0.05'
        line = output.strip()
        # Remove leading time and 'up'
        if ' up ' in line:
            line = line.split(' up ', 1)[1]

        result = {"uptime": "", "users": "", "load": ""}

        # Split off the load average first, since it contains its own commas
        # and would otherwise get shredded by a naive comma-split below.
        load_match = re.search(r'load averages?:\s*(.+)$', line, re.IGNORECASE)
        if load_match:
            result["load"] = f"load average: {load_match.group(1).strip()}"
            line = line[:load_match.start()]

        parts = [p.strip() for p in line.split(',') if p.strip()]
        if parts:
            result["uptime"] = parts[0]
            # Find the part with 'user' or 'users'
            for p in parts[1:]:
                if 'user' in p:
                    result["users"] = p
        return result

    def _uptime_from_proc():
        result = {"uptime": "", "users": "", "load": ""}
        try:
            with open('/proc/uptime', encoding='utf-8') as f:
                seconds = int(float(f.read().split()[0]))
            days, seconds = divmod(seconds, 86400)
            hours, seconds = divmod(seconds, 3600)
            minutes = seconds // 60
            parts = []
            if days:
                parts.append(f"{days} day{'s' if days != 1 else ''}")
            parts.append(f"{hours}:{minutes:02d}")
            result["uptime"] = ", ".join(parts)
        except Exception as e:
            logging.error(f"Failed reading /proc/uptime: {e}")

        try:
            with open('/proc/loadavg', encoding='utf-8') as f:
                load1, load5, load15 = f.read().split()[:3]
            result["load"] = f"load average: {load1}, {load5}, {load15}"
        except Exception as e:
            logging.error(f"Failed reading /proc/loadavg: {e}")

        return result


class Weather:

    def __init__(self, location):
        self.location = location
        self.weather = self.fetch_weather()

    def fetch_weather(self):
        url = f"https://wttr.in/{self.location}?format=j1"
        logging.info(f"iss-weather: Fetching weather for {self.location} at {url}")
        data = None
        icon = None
        try:
            response = requests.get(url, timeout=10)
            if response and response.content:
                try:
                    data = json.loads(response.content)
                    icon = self.icon(data["current_condition"][0]["weatherDesc"][0]["value"])
                except (ValueError, KeyError, IndexError) as e:
                    logging.error("weather: Failed to decode or parse data")
                    logging.error(e)
                    data = None
                    icon = None
            else:
                logging.error("Failed to fetch weather data: empty response")
        except requests.ReadTimeout as e:
            logging.error(f"weather: Timeout for request {e}")
        except Exception as e:
            logging.error(f"weather: Error requesting weather: {e}")
        return data, icon

    def icon(self, condition):
        icon = False

        if "Sunny" == condition:
            condition = "clear"
        elif "Clear" == condition:
            condition = "clear"

        fn = f"themes/default/weather/{condition}.svg"

        if not os.path.exists(fn):
            return False

        return fn

    def current_weather(self):
        # Always return a tuple for unpacking
        if self.weather is None:
            return None, None
        return self.weather

    @staticmethod
    def fetch_uv_index(location="Berlin"):
        """
        Fetches the UV index for the given location from wttr.in.
        Returns:
            str: The UV index as a string, or None on failure.
        """
        url = f"https://wttr.in/{location}?format=%u"
        try:
            response = requests.get(url, timeout=10)
            if response.status_code == 200:
                return response.text.strip()
            else:
                logging.error(f"Failed to fetch UV index: HTTP {response.status_code}")
        except Exception as e:
            logging.error(f"Error fetching UV index: {e}")
        return None

    @staticmethod
    def uv_risk(uv_index):
        try:
            uv = int(float(uv_index))
        except (TypeError, ValueError):
            return None

        levels = [
            (2,  "Low",       "No protection needed",            "No protection needed"),
            (5,  "Moderate",  "Sunscreen recommended",           "Sunscreen and hat recommended"),
            (7,  "High",      "Sunscreen essential, seek shade", "Limit midday exposure, hat and sunscreen"),
            (10, "Very High", "Avoid midday sun",                "Avoid midday sun, stay in shade"),
        ]

        for max_uv, label, adult, children in levels:
            if uv <= max_uv:
                return f"UV {uv} ({label}) — Adults: {adult}. Children: {children}."

        return f"UV {uv} (Extreme) — Adults: Avoid sun exposure. Children: Keep indoors during peak hours."