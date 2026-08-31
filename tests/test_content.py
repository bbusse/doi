'''
Tests for the content generation the display views draw from
'''

import json
import os
import unittest
from unittest import mock

import doi.main as main
from doi.main import (Art, Calendar, Music, News, OTD, PrometheusClient,
                      Weather)


class TestArtCaption(unittest.TestCase):
    def test_full_meta(self):
        caption = Art.caption({"title": "Irises", "artist": "van Gogh",
                               "date": "1889", "source": "the Met"})
        self.assertEqual(caption, "Irises, van Gogh, 1889 - the Met")

    def test_partial_meta(self):
        self.assertEqual(Art.caption({"title": "Irises"}), "Irises")
        self.assertEqual(Art.caption({"source": "the Met"}), "the Met")

    def test_meta_not_a_dict(self):
        self.assertEqual(Art.caption("Irises"), "Irises")
        self.assertEqual(Art.caption(None), "")


class TestCalendarMonth(unittest.TestCase):
    def test_month_shape(self):
        lines = Calendar.month_text()
        self.assertGreater(len(lines), 4)
        self.assertEqual(lines[1], "")
        self.assertIn("Mo", lines[2])

    def test_highlights_applied(self):
        import datetime
        lines = Calendar.month_text(lambda m: f"<{m.group(0)}>",
                                    lambda m: f"[{m.group(0)}]")
        text = "\n".join(lines)
        today = datetime.date.today()
        self.assertIn(f"<{str(today.day).rjust(2)}>", text)
        self.assertIn(f"[{today.strftime('%a')[:2]}]", text)

    def test_holiday_lines(self):
        holidays = [{"date": "2026-10-03", "name": "German Unity Day",
                     "localName": "Tag der Deutschen Einheit"}]
        with mock.patch.object(Calendar, "next_bank_holidays",
                               return_value=holidays):
            lines = Calendar.holiday_lines()
        self.assertEqual(lines, ["2026-10-03: German Unity Day "
                                 "(Tag der Deutschen Einheit)"])

    def test_holiday_lines_no_data(self):
        with mock.patch.object(Calendar, "next_bank_holidays",
                               return_value=None):
            self.assertEqual(Calendar.holiday_lines(), [])


class TestOTD(unittest.TestCase):
    @staticmethod
    def fetched(events):
        import datetime
        otd = OTD.__new__(OTD)
        otd.events = events
        otd.fetched_on = datetime.date.today()
        return otd

    def item(self, **kwargs):
        otd = self.fetched([dict(year=1969, text="Moon landing", **kwargs)])
        return otd.otd_item()

    def test_url_from_first_page(self):
        item = self.item(pages=[{"content_urls": {"desktop": {
            "page": "https://en.wikipedia.org/wiki/Apollo_11"}}}])
        self.assertEqual(item["url"],
                         "https://en.wikipedia.org/wiki/Apollo_11")

    def test_no_pages(self):
        self.assertEqual(self.item()["url"], "")

    def test_rotation(self):
        otd = self.fetched([{"year": 1, "text": "a"},
                            {"year": 2, "text": "b"}])
        years = [otd.otd_item()["year"] for _ in range(3)]
        self.assertEqual(years, [1, 2, 1])

    def test_empty(self):
        with mock.patch.object(main.requests, "get",
                               lambda url, **kw: Reply(404)):
            otd = OTD({"wikipedia": ""})
            self.assertIsNone(otd.otd_item())

    def test_lazy_and_dated(self):
        import datetime
        calls = []

        def get(url, **kw):
            calls.append(url)
            return Reply(200, {"events": [{"year": 1, "text": "a"}]})

        with mock.patch.object(main.requests, "get", get):
            otd = OTD({"wikipedia": ""})
            self.assertEqual(calls, [])
            otd.otd_item()
            otd.otd_item()
            self.assertEqual(len(calls), 1)
            # The date turning over triggers one refetch
            otd.fetched_on = datetime.date.today() - datetime.timedelta(days=1)
            otd.otd_item()
            self.assertEqual(len(calls), 2)

    def test_failed_refetch_keeps_events(self):
        import datetime
        with mock.patch.object(main.requests, "get",
                               lambda url, **kw: Reply(500)):
            otd = self.fetched([{"year": 1, "text": "a"}])
            otd.fetched_on = datetime.date.today() - datetime.timedelta(days=1)
            self.assertEqual(otd.otd_item()["year"], 1)


class Reply:
    def __init__(self, status, payload=None, content=b"", text=None,
                 headers=None):
        self.status_code = status
        self.payload = payload
        self.content = content
        self._text = text
        self.headers = headers or {}

    @property
    def text(self):
        if self._text is not None:
            return self._text
        if isinstance(self.content, bytes):
            return self.content.decode("utf-8", "replace")
        return self.content or ""

    def json(self):
        if self.payload is not None:
            return self.payload
        return json.loads(self.content)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise main.requests.HTTPError(f"HTTP {self.status_code}")


class TestCacheImage(unittest.TestCase):
    def test_no_url(self):
        self.assertEqual(main.cache_image("", "x-"), "")

    def test_fetch_and_reuse(self):
        import tempfile
        tmp = tempfile.mkdtemp(prefix="doi-img-")
        calls = []

        def get(url, **kw):
            calls.append(url)
            return Reply(200, content=b"jpegbytes")

        with mock.patch.object(main.requests, "get", get):
            first = main.cache_image("http://img/a.jpg", "x-", tmp)
            second = main.cache_image("http://img/a.jpg", "x-", tmp)

        self.assertEqual(first, second)
        self.assertEqual(open(first, "rb").read(), b"jpegbytes")
        self.assertEqual(len(calls), 1)

    def test_fetch_failure(self):
        import tempfile
        with mock.patch.object(main.requests, "get",
                               lambda url, **kw: Reply(500)):
            self.assertEqual(
                main.cache_image("http://img/a.jpg", "x-",
                                 tempfile.mkdtemp(prefix="doi-img-")), "")


class TestApod(unittest.TestCase):
    def test_meta_shape_and_caption(self):
        import tempfile
        from doi.main import APOD, Art

        payload = {"title": "Eclipse Pair",
                   "copyright": " Jane Doe ",
                   "date": "2026-08-29",
                   "explanation": "Two eclipses.",
                   "hdurl": "http://img/apod.jpg"}

        def get(url, **kw):
            if "api.nasa.gov" in url:
                return Reply(200, payload)
            return Reply(200, content=b"jpegbytes")

        with mock.patch.object(main.requests, "get", get):
            apod = APOD(save_dir=tempfile.mkdtemp(prefix="doi-test-apod-"))
            img_path, meta = apod.apod_data()

        self.assertTrue(img_path.endswith(".jpg"))
        self.assertEqual(open(img_path, "rb").read(), b"jpegbytes")
        self.assertEqual(
            Art.caption(meta),
            "Eclipse Pair, Jane Doe, 2026-08-29 - NASA APOD")
        self.assertEqual(meta["description"], "Two eclipses.")

    def test_video_day_no_image_keeps_meta(self):
        from doi.main import APOD

        payload = {"title": "A Launch", "date": "2026-08-31",
                   "explanation": "Rocket goes up.", "media_type": "video",
                   "url": "https://apod.nasa.gov/apod/image/x/launch.mp4",
                   "thumbnail_url": ""}
        with mock.patch.object(main.requests, "get",
                               lambda url, **kw: Reply(200, payload)):
            img_path, meta = APOD().apod_data()

        self.assertIsNone(img_path)
        self.assertEqual(meta["title"], "A Launch")
        self.assertEqual(meta["description"], "Rocket goes up.")


SPOTIFY_PLAYING = {
    "is_playing": True,
    "progress_ms": 1000,
    "item": {"name": "Paranoid Android",
             "duration_ms": 383000,
             "artists": [{"name": "Radiohead"}],
             "album": {"name": "OK Computer",
                       "images": [{"url": "http://img/640"},
                                  {"url": "http://img/300"},
                                  {"url": "http://img/64"}]},
             "external_urls": {
                 "spotify": "https://open.spotify.com/track/abc"}}}


class TestSpotify(unittest.TestCase):
    env = {"SPOTIFY_CLIENT_ID": "cid",
           "SPOTIFY_CLIENT_SECRET": "csec",
           "SPOTIFY_REFRESH_TOKEN": "rtok"}

    def test_unconfigured(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(Music.spotify_configured())
            self.assertIsNone(Music().spotify())

    def test_playing(self):
        posts, gets = [], []

        def post(url, **kw):
            posts.append(kw)
            return Reply(200, {"access_token": "atok", "expires_in": 3600})

        def get(url, **kw):
            gets.append((url, kw))
            if "img" in url:
                return Reply(200, content=b"jpeg")
            return Reply(200, SPOTIFY_PLAYING)

        import tempfile
        art_dir = tempfile.mkdtemp(prefix="doi-test-art-")
        with mock.patch.dict(os.environ, self.env), \
             mock.patch.object(main.requests, "post", post), \
             mock.patch.object(main.requests, "get", get), \
             mock.patch.object(main.tempfile, "gettempdir",
                               return_value=art_dir):
            music = Music()
            item = music.spotify()
            self.assertEqual(item["artists"], "Radiohead")
            self.assertEqual(item["title"], "Paranoid Android")
            self.assertEqual(item["url"],
                             "https://open.spotify.com/track/abc")
            self.assertTrue(item["art_file"])
            # The mid-size image serves the overlay
            self.assertIn("img/300", gets[1][0])
            # A second poll reuses the token and the cached art
            music.spotify()
            self.assertEqual(len(posts), 1)
            self.assertEqual(
                len([g for g in gets if "img" in g[0]]), 1)

    def test_nothing_playing(self):
        with mock.patch.dict(os.environ, self.env), \
             mock.patch.object(main.requests, "post", lambda url, **kw:
                               Reply(200, {"access_token": "atok",
                                           "expires_in": 3600})), \
             mock.patch.object(main.requests, "get", lambda url, **kw:
                               Reply(204)):
            self.assertEqual(Music().spotify(), {})

    def test_token_refused(self):
        with mock.patch.dict(os.environ, self.env), \
             mock.patch.object(main.requests, "post", lambda url, **kw:
                               Reply(400)):
            self.assertIsNone(Music().spotify())


WTTR = {"current_condition": [{"temp_C": "21",
                               "weatherDesc": [{"value": "Sunny"}],
                               "windspeedKmph": "11"}],
        "nearest_area": [{"areaName": [{"value": "Berlin"}]}]}


class TestWeatherReport(unittest.TestCase):
    def weather(self, data, icon=None):
        weather = Weather.__new__(Weather)
        weather.location = "Berlin"
        weather.weather = (data, icon)
        return weather

    def test_report(self):
        with mock.patch.object(Calendar, "sunrise_sunset", return_value={
                    "today": {"sunrise": "06:10", "sunset": "20:05"}}), \
             mock.patch.object(Calendar, "moonphase",
                               return_value=("Full Moon", None, 0)), \
             mock.patch.object(Weather, "fetch_uv_index",
                               return_value="5"):
            texts, icon = self.weather(WTTR).report()
        self.assertIn("21°C", texts)
        self.assertIn("Sunny 11 km/h", texts)
        self.assertIn("Berlin", texts)
        self.assertIn("Sunrise 06:10  Sunset 20:05", texts)
        self.assertTrue(any("Full Moon" in t for t in texts))
        self.assertTrue(any("UV Index 5 (Moderate)" in t for t in texts))
        self.assertTrue(any("Children: Sunscreen and hat recommended" in t
                            for t in texts))

    def test_uv_risk(self):
        self.assertIn("(Low)", Weather.uv_risk("1"))
        self.assertIn("(Moderate)", Weather.uv_risk(5))
        self.assertIn("(High)", Weather.uv_risk("6.4"))
        self.assertIn("(Very High)", Weather.uv_risk(9))
        self.assertIn("(Extreme)", Weather.uv_risk(11))
        self.assertIsNone(Weather.uv_risk(None))
        self.assertIsNone(Weather.uv_risk("n/a"))

    def test_report_no_data(self):
        with mock.patch.object(Calendar, "sunrise_sunset",
                               return_value=None), \
             mock.patch.object(Calendar, "moonphase",
                               return_value=(None, None, None)), \
             mock.patch.object(Weather, "fetch_uv_index",
                               return_value=None):
            texts, icon = self.weather(None).report()
        self.assertIn("Weather data unavailable", texts)
        self.assertIsNone(icon)


class TestArtNGA(unittest.TestCase):
    # uuid, iiifurl, iiifthumburl, viewtype, sequence, width, height,
    # maxpixels, openaccess, created, modified, tmsid, assistivetext
    row = ("abcd-1234,iiif,thumb,primary,0,4000,3000,0,1,"
           "2020-01-01,2021-01-01,12345,A field of irises")

    def test_random_row_parsed(self):
        nga = main.ArtNGA.__new__(main.ArtNGA)
        nga._size = 100000
        with mock.patch.object(main.requests, "get", lambda url, **kw:
                               Reply(200, text="fragment-of-a-row\n"
                                     + self.row + "\n")):
            row = nga.random_row()
        self.assertEqual(row, {"uuid": "abcd-1234", "objectid": "12345",
                               "text": "A field of irises"})

    def test_art_data_happy_path(self):
        import tempfile

        def head(url, **kw):
            return Reply(200, headers={"content-length": "100000"})

        def get(url, **kw):
            if "published_images.csv" in url:
                return Reply(200, text="fragment\n" + self.row + "\n")
            return Reply(200, content=b"jpegbytes")

        meta = {"title": "Irises", "artist": "van Gogh", "date": "1889",
                "medium": "oil on canvas"}
        with mock.patch.object(main.requests, "head", head), \
             mock.patch.object(main.requests, "get", get), \
             mock.patch.object(main.ArtNGA, "object_meta", return_value=meta):
            path, out = main.ArtNGA(
                save_dir=tempfile.mkdtemp(prefix="doi-test-nga-")).art_data()

        self.assertTrue(path.endswith("art-nga.jpg"))
        self.assertEqual(open(path, "rb").read(), b"jpegbytes")
        self.assertEqual(out["source"], "National Gallery of Art")
        self.assertEqual(out["description"], "A field of irises")
        self.assertEqual(
            Art.caption(out),
            "Irises, van Gogh, 1889 - National Gallery of Art")


class TestArtMet(unittest.TestCase):
    OBJECT = {"title": "Wheat Field with Cypresses",
              "artistDisplayName": "Vincent van Gogh",
              "objectDate": "1889",
              "medium": "Oil on canvas",
              "primaryImageSmall": "https://images.metmuseum.org/x/met.jpg",
              "creditLine": "Purchase, 1993"}

    def test_art_data_happy_path(self):
        import tempfile

        def get(url, **kw):
            if "/search" in url:
                return Reply(200, {"objectIDs": [436535]})
            if "/objects/" in url:
                return Reply(200, self.OBJECT)
            return Reply(200, content=b"jpegbytes")

        with mock.patch.object(main.requests, "get", get):
            path, work = main.ArtMet(
                save_dir=tempfile.mkdtemp(prefix="doi-test-met-")).art_data()

        self.assertTrue(path.endswith("art-met.jpg"))
        self.assertEqual(open(path, "rb").read(), b"jpegbytes")
        self.assertEqual(work["source"], "The Metropolitan Museum of Art")
        self.assertEqual(
            Art.caption(work),
            "Wheat Field with Cypresses, Vincent van Gogh, 1889"
            " - The Metropolitan Museum of Art")


class TestBankHolidays(unittest.TestCase):
    def test_next_bank_holidays(self):
        import datetime
        today = datetime.date.today()

        def day(offset):
            return (today + datetime.timedelta(days=offset)).isoformat()

        payload = [
            {"date": day(-5), "localName": "Past", "name": "Past"},
            {"date": day(10), "localName": "Tag der Einheit",
             "name": "German Unity Day"},
            {"date": day(20), "localName": "Reformationstag",
             "name": "Reformation Day"},
            {"date": day(40), "localName": "1. Weihnachtstag",
             "name": "Christmas Day"},
        ]
        with mock.patch.object(main.requests, "get",
                               lambda url, **kw: Reply(200, payload)):
            out = Calendar.next_bank_holidays(location="Germany", count=3)

        self.assertEqual([h["name"] for h in out],
                         ["German Unity Day", "Reformation Day",
                          "Christmas Day"])
        self.assertEqual(out[0]["localName"], "Tag der Einheit")


J1 = {"current_condition": [{"temp_C": "21", "windspeedKmph": "11",
                             "weatherDesc": [{"value": "Sunny"}]}],
      "nearest_area": [{"areaName": [{"value": "Berlin"}]}],
      "weather": [
          {"date": "2026-08-31",
           "astronomy": [{"sunrise": "06:10 AM", "sunset": "08:05 PM",
                          "moon_phase": "Waxing Gibbous"}]},
          {"date": "2026-09-01",
           "astronomy": [{"sunrise": "06:12 AM", "sunset": "08:03 PM",
                          "moon_phase": "Full Moon"}]},
      ]}


class TestWttrSources(unittest.TestCase):
    # wttr.in's j1 payload is read via .json() by moonphase / sunrise_sunset
    # but via .content by Weather.fetch_weather, so the fake serves both
    def wttr(self, payload=J1):
        return lambda url, **kw: Reply(200, payload,
                                       content=json.dumps(payload).encode())

    def test_moonphase(self):
        with mock.patch.object(main.requests, "get", self.wttr()):
            phase, icon, days = Calendar.moonphase(location="Berlin")
        self.assertEqual(phase, "Waxing Gibbous")
        self.assertTrue(icon)

    def test_moonphase_days_to_full(self):
        import copy
        import datetime
        j1 = copy.deepcopy(J1)
        full = datetime.date.today() + datetime.timedelta(days=6)
        j1["weather"][1]["date"] = full.isoformat()
        with mock.patch.object(main.requests, "get", self.wttr(j1)):
            _, _, days = Calendar.moonphase(location="Berlin")
        self.assertEqual(days, 6)

    def test_sunrise_sunset(self):
        with mock.patch.object(main.requests, "get", self.wttr()):
            out = Calendar.sunrise_sunset(location="Berlin")
        self.assertEqual(out["today"],
                         {"sunrise": "06:10 AM", "sunset": "08:05 PM"})
        self.assertEqual(out["tomorrow"]["sunrise"], "06:12 AM")

    def test_fetch_weather_via_constructor(self):
        with mock.patch.object(main.requests, "get", self.wttr()):
            weather = Weather("Berlin")
        data, _ = weather.current_weather()
        self.assertEqual(data["current_condition"][0]["temp_C"], "21")

    def test_fetch_uv_index(self):
        with mock.patch.object(main.requests, "get", lambda url, **kw:
                               Reply(200, text="5\n")):
            self.assertEqual(Weather.fetch_uv_index(location="Berlin"), "5")


RSS_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>Example News</title>
  <item><title>First headline</title><link>https://example.com/1</link></item>
  <item><title>Second headline</title><link>https://example.com/2</link></item>
</channel></rss>"""


class TestRSS(unittest.TestCase):
    def test_news_rss_fetch(self):
        with mock.patch.object(main.requests, "get",
                               lambda url, **kw: Reply(200, content=RSS_XML)):
            news = News({"rss": "https://example.com/feed.xml"})
        titles = sorted(n["title"] for n in news.news)
        self.assertEqual(titles, ["First headline", "Second headline"])
        self.assertTrue(all(n["feed"] == "Example News" for n in news.news))

    def test_rssfeed_item(self):
        from doi.main import RSSFeed
        with mock.patch.object(main.requests, "get",
                               lambda url, **kw: Reply(200, content=RSS_XML)):
            feed = RSSFeed("https://example.com/feed.xml")
            item = feed.news_item()
        self.assertEqual(feed.item_count(), 2)
        self.assertEqual(item["feed"], "Example News")
        self.assertIn(item["title"], ("First headline", "Second headline"))
        self.assertIn("rank", item)


# app.bsky.feed.getAuthorFeed trimmed to what Bluesky reads. embed is the
# hydrated view getAuthorFeed puts on the post, carrying cdn urls, not the
# blob only record embed
#   aaa111  external link card with a preview thumb
#   bbb222  a repost, skipped by default
#   ccc333  whitespace only text, skipped
#   ddd444  a real picture, fullsize and alt
BSKY_FEED = {"feed": [
    {"post": {
        "uri": "at://did:plc:ft/app.bsky.feed.post/aaa111",
        "author": {"handle": "financialtimes.com",
                   "displayName": "Financial Times"},
        "record": {"text": "Markets wobble as yields climb ft.trib.al/abc",
                   "createdAt": "2026-08-31T07:43:27.799Z"},
        "embed": {"$type": "app.bsky.embed.external#view",
                  "external": {"uri": "https://ft.trib.al/abc",
                               "title": "Markets wobble",
                               "thumb": "https://cdn.bsky.app/img/card.jpg"}}}},
    {"reason": {"$type": "app.bsky.feed.defs#reasonRepost"},
     "post": {"uri": "at://did:plc:edit/app.bsky.feed.post/bbb222",
              "author": {"handle": "ftedit.ft.com", "displayName": "FT Edit"},
              "record": {"text": "A repost, skipped by default",
                         "createdAt": "2026-08-31T06:00:00.000Z"}}},
    {"post": {
        "uri": "at://did:plc:ft/app.bsky.feed.post/ccc333",
        "author": {"handle": "financialtimes.com",
                   "displayName": "Financial Times"},
        "record": {"text": "   ", "createdAt": "2026-08-31T05:00:00.000Z"}}},
    {"post": {
        "uri": "at://did:plc:ft/app.bsky.feed.post/ddd444",
        "author": {"handle": "financialtimes.com",
                   "displayName": "Financial Times"},
        "record": {"text": "Chart of the day: other income surges",
                   "createdAt": "2026-08-31T04:00:00.000Z"},
        "embed": {"$type": "app.bsky.embed.images#view",
                  "images": [{"thumb": "https://cdn.bsky.app/img/t/1.jpg",
                              "fullsize": "https://cdn.bsky.app/img/f/1.jpg",
                              "alt": "Bar chart of other income"}]}}},
]}


class TestBluesky(unittest.TestCase):
    def transport(self, payload=BSKY_FEED, image=b"jpegbytes"):
        calls = []

        def get(url, **kw):
            if "getAuthorFeed" in url:
                calls.append(kw.get("params", {}))
                return Reply(200, payload)
            calls.append(url)           # an image download
            return Reply(200, content=image)

        return get, calls

    def mk(self, actor="financialtimes.com", **kw):
        import tempfile
        kw.setdefault("image_dir", tempfile.mkdtemp(prefix="doi-bsky-"))
        return main.Bluesky(actor, **kw)

    @staticmethod
    def rkey(item):
        return item["url"].rsplit("/", 1)[-1]

    def test_parses_and_builds_permalink(self):
        get, _ = self.transport()
        with mock.patch.object(main.requests, "get", get):
            bsky = self.mk("https://bsky.app/profile/financialtimes.com")
            self.assertEqual(bsky.item_count(), 0)   # nothing fetched yet
            item = bsky.post_item()
        self.assertEqual(item["handle"], "financialtimes.com")
        self.assertEqual(item["feed"], "Financial Times")
        self.assertEqual(
            item["url"],
            "https://bsky.app/profile/financialtimes.com/post/aaa111")
        self.assertEqual(item["link"], "https://ft.trib.al/abc")
        self.assertEqual(item["image_url"], "https://cdn.bsky.app/img/card.jpg")
        self.assertEqual(item["image_alt"], "Markets wobble")
        self.assertEqual(item["text"], item["title"])
        self.assertEqual(item["rank"], 1)
        self.assertTrue(item["created_at"])

    def test_actor_normalised_and_query(self):
        get, calls = self.transport()
        with mock.patch.object(main.requests, "get", get):
            self.mk("https://bsky.app/profile/financialtimes.com/").post_item()
        self.assertEqual(calls[0]["actor"], "financialtimes.com")
        self.assertEqual(calls[0]["filter"], "posts_no_replies")

    def test_skips_reposts_and_blank_posts(self):
        get, _ = self.transport()
        with mock.patch.object(main.requests, "get", get):
            bsky = self.mk()
            bsky.fetch()
        self.assertEqual([self.rkey(i) for i in bsky._items],
                         ["aaa111", "ddd444"])

    def test_include_reposts(self):
        get, _ = self.transport()
        with mock.patch.object(main.requests, "get", get):
            bsky = self.mk(include_reposts=True)
            bsky.fetch()
        self.assertEqual([self.rkey(i) for i in bsky._items],
                         ["aaa111", "bbb222", "ddd444"])

    def test_rotates(self):
        get, _ = self.transport()
        with mock.patch.object(main.requests, "get", get):
            bsky = self.mk()
            seen = [self.rkey(bsky.post_item()) for _ in range(3)]
        self.assertEqual(seen, ["aaa111", "ddd444", "aaa111"])

    def test_image_post_downloads_and_caches(self):
        get, calls = self.transport()
        with mock.patch.object(main.requests, "get", get):
            bsky = self.mk()
            bsky.post_item()                 # aaa111
            item = bsky.post_item()          # ddd444, the picture
            self.assertEqual(item["image_url"], "https://cdn.bsky.app/img/f/1.jpg")
            self.assertTrue(item["image"].endswith(".jpg"))
            self.assertEqual(open(item["image"], "rb").read(), b"jpegbytes")
            downloads = [c for c in calls if isinstance(c, str)]
            # rotate all the way back round to the same post
            for _ in range(len(bsky._items)):
                again = bsky.post_item()
                if again["url"] == item["url"]:
                    break
            self.assertEqual(again["image"], item["image"])
            # the cached file is reused, not fetched again
            self.assertEqual([c for c in calls if isinstance(c, str)], downloads)

    def test_image_fetch_failure_leaves_post(self):
        def get(url, **kw):
            if "getAuthorFeed" in url:
                return Reply(200, BSKY_FEED)
            return Reply(500)
        with mock.patch.object(main.requests, "get", get):
            bsky = self.mk()
            bsky.post_item()
            item = bsky.post_item()          # ddd444
        self.assertEqual(item["image"], "")
        self.assertEqual(item["image_url"], "https://cdn.bsky.app/img/f/1.jpg")

    def test_embed_shapes(self):
        embed = main.Bluesky._embed
        # quote post with media, picture comes off the media
        rwm = {"$type": "app.bsky.embed.recordWithMedia#view",
               "media": {"$type": "app.bsky.embed.images#view",
                         "images": [{"fullsize": "u/full.jpg", "alt": "a cat"}]}}
        self.assertEqual(embed(rwm), ("", "u/full.jpg", "a cat"))
        # video, the poster frame stands in
        vid = {"$type": "app.bsky.embed.video#view",
               "thumbnail": "u/poster.jpg"}
        self.assertEqual(embed(vid), ("", "u/poster.jpg", ""))
        # nothing attached
        self.assertEqual(embed({}), ("", "", ""))

    def test_lazy_and_cached_within_ttl(self):
        get, calls = self.transport()
        with mock.patch.object(main.requests, "get", get):
            bsky = self.mk()
            self.assertEqual(calls, [])
            bsky.post_item()
            bsky.post_item()
        feed_calls = [c for c in calls if not isinstance(c, str)]
        self.assertEqual(len(feed_calls), 1)

    def test_failed_refetch_keeps_posts(self):
        get, _ = self.transport()
        with mock.patch.object(main.requests, "get", get):
            bsky = self.mk()
            bsky.fetch()
        # age the cache past its TTL, then let the refetch fail
        bsky._fetched_at -= bsky.refetch_after_s + 1
        with mock.patch.object(main.requests, "get",
                               lambda url, **kw: Reply(502)):
            item = bsky.post_item()
        self.assertIsNotNone(item)
        self.assertEqual(bsky.item_count(), 2)


class TestNewsSqlite(unittest.TestCase):
    def test_sqlite_select(self):
        import sqlite3
        import tempfile
        path = os.path.join(tempfile.mkdtemp(prefix="doi-test-news-"), "n.db")
        con = sqlite3.connect(path)
        con.executescript("""
            CREATE TABLE feeds (id INTEGER PRIMARY KEY, title TEXT);
            CREATE TABLE entries (id INTEGER PRIMARY KEY, feed_id INTEGER,
                title TEXT, link TEXT, pub_date TEXT);
            INSERT INTO feeds VALUES (1, 'Feed A');
            INSERT INTO entries VALUES
                (1, 1, 'Older entry', 'https://a/1', '2026-01-01'),
                (2, 1, 'Newer entry', 'https://a/2', '2026-02-01');
        """)
        con.commit()
        con.close()

        news = News({"db": path})
        item = news.news_item()
        self.assertEqual(item["feed"], "Feed A")
        # Query orders by pub_date DESC, so the newest entry comes first
        self.assertEqual(item["title"], "Newer entry")
        self.assertEqual(item["url"], "https://a/2")


class TestMQTT(unittest.TestCase):
    def message_handler(self, topic, payload):
        from doi.main import MQTT
        captured = []
        client = mock.Mock()
        MQTT("broker", "cid").subscribe(
            client, topic, lambda t, texts: captured.append((t, texts)))
        on_message = client.message_callback_add.call_args[0][1]
        on_message(client, None, mock.Mock(topic=topic, payload=payload))
        return captured

    def test_current_song(self):
        captured = self.message_handler(
            "hyperblast/current_song",
            b'{"title": "Paranoid Android", "file": "radiohead.flac"}')
        self.assertEqual(captured,
                         [("hyperblast/current_song",
                           ["Paranoid Android", "[radiohead.flac]"])])

    def test_temperature_sensor(self):
        captured = self.message_handler(
            "sensor/mainhallsensor/temperature", b'{"value": 21.4}')
        self.assertEqual(captured[0][1], ["Mainhall", "21.4 °C"])

    def test_plain_text_payload(self):
        captured = self.message_handler("some/other/topic", b"just text")
        self.assertEqual(captured[0][1], ["just text"])


class TestPrometheusClient(unittest.TestCase):
    sample = (
        "# HELP requests_total Requests handled\n"
        "# TYPE requests_total counter\n"
        'requests_total{path="/api"} 1150\n'
        'requests_total{path="/metrics"} 1\n'
        "up 1\n"
        'build_info{version="0.1 beta"} 1\n'
        'odd{note="a } brace"} 3\n'
    )

    def parsed(self, *names):
        with mock.patch("doi.main.requests.get") as get:
            get.return_value = mock.Mock(text=self.sample,
                                         raise_for_status=lambda: None)
            return PrometheusClient("host:9000/metrics").values(*names)

    def test_url_scheme_added(self):
        self.assertEqual(PrometheusClient("host:9000/metrics").url,
                         "http://host:9000/metrics")
        self.assertEqual(PrometheusClient("https://host/m").url,
                         "https://host/m")

    def test_named_metric_all_series(self):
        self.assertEqual(self.parsed("requests_total"),
                         {'requests_total{path="/api"}': 1150.0,
                          'requests_total{path="/metrics"}': 1.0})

    def test_unlabelled_metric(self):
        self.assertEqual(self.parsed("up"), {"up": 1.0})

    def test_label_value_with_spaces_and_brace(self):
        self.assertEqual(self.parsed("build_info", "odd"),
                         {'build_info{version="0.1 beta"}': 1.0,
                          'odd{note="a } brace"}': 3.0})

    def test_no_names_returns_everything(self):
        self.assertEqual(len(self.parsed()), 5)

    def test_value_sums_series(self):
        with mock.patch("doi.main.requests.get") as get:
            get.return_value = mock.Mock(text=self.sample,
                                         raise_for_status=lambda: None)
            self.assertEqual(
                PrometheusClient("h/m").value("requests_total"), 1151.0)

    def test_fetch_failure_is_empty(self):
        with mock.patch("doi.main.requests.get",
                        side_effect=main.requests.RequestException("boom")):
            self.assertEqual(PrometheusClient("h/m").values("up"), {})


def unreachable(*args, **kwargs):
    '''A requests stand-in for an upstream that is down'''
    raise main.requests.ConnectionError("upstream unreachable")


class TestDegradation(unittest.TestCase):
    '''
    Every source asked for data while its upstream is down. None may raise,
    a dead feed drops its panel from the view without taking the process
    with it
    '''

    def test_apod_http_error(self):
        import tempfile
        from doi.main import APOD
        with mock.patch.object(main.requests, "get",
                               lambda url, **kw: Reply(503)):
            out = APOD(save_dir=tempfile.mkdtemp(prefix="doi-t-")).apod_data()
        self.assertEqual(out, (None, None))

    def test_art_nga_unreachable(self):
        import tempfile
        with mock.patch.object(main.requests, "head", unreachable), \
             mock.patch.object(main.requests, "get", unreachable):
            out = main.ArtNGA(
                save_dir=tempfile.mkdtemp(prefix="doi-t-")).art_data()
        self.assertEqual(out, (None, None))

    def test_art_met_no_images(self):
        import tempfile
        with mock.patch.object(main.requests, "get", lambda url, **kw:
                               Reply(200, {"objectIDs": []})):
            out = main.ArtMet(
                save_dir=tempfile.mkdtemp(prefix="doi-t-")).art_data()
        self.assertEqual(out, (None, None))

    def test_spotify_currently_playing_error(self):
        with mock.patch.dict(os.environ, TestSpotify.env), \
             mock.patch.object(main.requests, "post", lambda url, **kw:
                               Reply(200, {"access_token": "atok",
                                           "expires_in": 3600})), \
             mock.patch.object(main.requests, "get",
                               lambda url, **kw: Reply(500)):
            self.assertIsNone(Music().spotify())

    def test_bank_holidays_http_error(self):
        with mock.patch.object(main.requests, "get",
                               lambda url, **kw: Reply(500)):
            self.assertIsNone(Calendar.next_bank_holidays(location="Germany"))

    def test_bank_holidays_bad_country(self):
        self.assertIsNone(Calendar.next_bank_holidays(location="Neverland"))

    def test_moonphase_unreachable(self):
        with mock.patch.object(main.requests, "get", unreachable):
            self.assertEqual(Calendar.moonphase(location="Berlin"),
                             (None, None, None))

    def test_sunrise_sunset_unreachable(self):
        with mock.patch.object(main.requests, "get", unreachable):
            self.assertIsNone(Calendar.sunrise_sunset(location="Berlin"))

    def test_weather_unreachable(self):
        with mock.patch.object(main.requests, "get", unreachable):
            weather = Weather("Berlin")
        self.assertEqual(weather.current_weather(), (None, None))
        # report() still yields lines rather than raising
        texts, icon = weather.report()
        self.assertIn("Weather data unavailable", texts)

    def test_uv_index_http_error(self):
        with mock.patch.object(main.requests, "get",
                               lambda url, **kw: Reply(500)):
            self.assertIsNone(Weather.fetch_uv_index(location="Berlin"))

    def test_news_rss_unreachable(self):
        with mock.patch.object(main.requests, "get", unreachable):
            news = News({"rss": "https://example.com/feed.xml"})
        self.assertEqual(news.news, [])
        self.assertIsNone(news.news_item())

    def test_news_db_missing(self):
        news = News({"db": "/no/such/news.db"})
        self.assertEqual(news.news, [])
        self.assertIsNone(news.news_item())

    def test_rssfeed_unreachable(self):
        from doi.main import RSSFeed
        with mock.patch.object(main.requests, "get", unreachable):
            feed = RSSFeed("https://example.com/feed.xml")
            self.assertIsNone(feed.news_item())
        self.assertEqual(feed.item_count(), 0)

    def test_otd_unreachable(self):
        with mock.patch.object(main.requests, "get", unreachable):
            self.assertIsNone(OTD({"wikipedia": ""}).otd_item())

    def test_bluesky_unreachable(self):
        from doi.main import Bluesky
        with mock.patch.object(main.requests, "get", unreachable):
            self.assertIsNone(Bluesky("financialtimes.com").post_item())

    def test_prometheus_unreachable(self):
        with mock.patch.object(main.requests, "get", unreachable):
            self.assertEqual(PrometheusClient("h/m").values(), {})
            self.assertIsNone(PrometheusClient("h/m").value("up"))


# ArtNGA.object_meta bisects byte offsets into an objects.csv it never
# downloads whole. The happy path test stubs it out, this drives the search
# itself against a synthetic sorted slice served through ranged GETs
class TestArtNGAObjectMeta(unittest.TestCase):
    @staticmethod
    def build_csv(count=500, step=3, first=1000):
        # 31 columns, ids ascending, title/date/medium/artist at the offsets
        # object_meta reads (5, 6, 10, 15)
        rows = [",".join(f"col{i}" for i in range(31))]
        for k in range(count):
            oid = first + k * step
            f = [""] * 31
            f[0] = str(oid)
            f[5] = f"Work number {oid}"
            f[6] = f"18{oid % 100:02d}"
            f[10] = "oil on canvas"
            f[15] = f"Painter {oid}"
            rows.append(",".join(f))
        return ("\n".join(rows) + "\n").encode()

    def transport(self, csv_bytes):
        def head(url, **kw):
            return Reply(200,
                         headers={"content-length": str(len(csv_bytes))})

        def get(url, **kw):
            # "bytes=START-END", inclusive, as ArtNGA builds it
            span = kw["headers"]["Range"].split("=")[1]
            start, end = (int(x) for x in span.split("-"))
            chunk = csv_bytes[start:end + 1]
            return Reply(206, content=chunk)

        return head, get

    def nga(self):
        obj = main.ArtNGA.__new__(main.ArtNGA)
        obj._size = None
        obj._objects_size = None
        return obj

    def test_finds_the_row(self):
        csv_bytes = self.build_csv()
        head, get = self.transport(csv_bytes)
        with mock.patch.object(main.requests, "head", head), \
             mock.patch.object(main.requests, "get", get):
            meta = self.nga().object_meta("2200")   # first + 400*step
        self.assertEqual(meta, {"title": "Work number 2200", "date": "1800",
                                "medium": "oil on canvas",
                                "artist": "Painter 2200"})

    def test_absent_id_gives_empty(self):
        csv_bytes = self.build_csv()
        head, get = self.transport(csv_bytes)
        with mock.patch.object(main.requests, "head", head), \
             mock.patch.object(main.requests, "get", get):
            self.assertEqual(self.nga().object_meta("2201"), {})  # between rows

    def test_non_numeric_id(self):
        self.assertEqual(self.nga().object_meta(None), {})
        self.assertEqual(self.nga().object_meta("not-a-number"), {})


if __name__ == "__main__":
    unittest.main()
