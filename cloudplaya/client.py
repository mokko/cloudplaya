import cookielib
import json
import logging
import os
import math
import random
import re

import mechanize
import requests

from cloudplaya.album import Album
from cloudplaya.artist import Artist
from cloudplaya.song import Song


class RequestError(Exception):
    def __init__(self, msg, code):
        super(Exception, self).__init__(msg)
        self.code = code


class Client(object):
    PLAYER_URL = 'https://www.amazon.com/gp/dmusic/mp3/player'
    API_URL = 'https://www.amazon.com/cirrus/'

    CUSTOMER_ID_RE = re.compile(r'amznMusic.customerId\s*=\s*[\'"](.*)[\'"];')
    ADP_TOKEN_RE = re.compile(r'amznMusic.tid\s*=\s[\'"](.*)[\'"];')
    DEVICE_ID_RE = re.compile(r'amznMusic.did\s*=\s*[\'"](.*)[\'"];')
    DEVICE_TYPE_RE = re.compile(r'amznMusic.dtid\s*=\s*[\'"](.*)[\'"];')

    DEFAULT_SONG_SORT = [('sortTitle', 'ASC')]
    SONG_SEARCH = [
        ('keywords', 'LIKE', ''),
        ('assetType', 'EQUALS', 'AUDIO'),
        ('status', 'EQUALS', 'AVAILABLE'),
    ]

    DEFAULT_ARTIST_SORT = [('sortArtistName', 'ASC')]
    ARTIST_SEARCH = [
        ('status', 'EQUALS', 'AVAILABLE'),
        ('trackStatus', 'IS_NULL'),
    ]

    DEFAULT_ALBUM_SORT = [('sortAlbumName', 'ASC')]
    ALBUM_SEARCH = [
        ('status', 'EQUALS', 'AVAILABLE'),
    ]

    PAGINATE_BY = 50

    USER_AGENT = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/535.19 ' \
                 '(KHTML, like Gecko) Chrome/18.0.1025.142 Safari/535.19'
    REFERER = 'https://www.amazon.com/gp/dmusic/mp3/player?ie=UTF8&' \
              'ref_=gno_yam_cldplyr&'
    ORIGIN = 'https://www.amazon.com'

    def __init__(self):
        self.customer_id = None
        self.adp_token = None
        self.device_id = None
        self.device_type = None
        self.cookies = None
        self.authenticated = False

        self._load_config()

    def authenticate(self, username, password):
        browser = mechanize.Browser(factory=mechanize.RobustFactory())
        cookiejar = cookielib.LWPCookieJar()
        browser.set_cookiejar(cookiejar)

        browser.set_handle_equiv(True)
        browser.set_handle_redirect(True)
        browser.set_handle_referer(True)
        browser.set_handle_robots(False)
        browser.set_handle_refresh(mechanize._http.HTTPRefreshProcessor(),
                                   max_time=1)
        browser.addheaders = [('User-agent', self.USER_AGENT)]

        # Attempt to log in to Amazon.
        # Note: We should end up with a redirect.
        r = browser.open(self.PLAYER_URL)

        browser.select_form(name="signIn")
        browser.form['email'] = username
        browser.form['password'] = password
        browser.form['create'] = False
        browser.submit()

        content = browser.response().read()

        # Get all the amznMusic variables being set.
        auth_vars = {
            'customer_id': None,
            'adp_token': None,
            'device_id': None,
            'device_type': None,
        }

        vars_map = (('customer_id', self.CUSTOMER_ID_RE),
                    ('adp_token', self.ADP_TOKEN_RE),
                    ('device_id', self.DEVICE_ID_RE),
                    ('device_type', self.DEVICE_TYPE_RE))

        # Record how many of the necessary lines we've found.
        # At 3, we can bail.
        found = 0

        for line in content.splitlines():
            line = line.strip()

            if line.startswith('amznMusic.'):
                for key, regex in vars_map:
                    m = regex.match(line)

                    if m:
                        found += 1
                        auth_vars[key] = m.group(1)
                        break

                if found == len(auth_vars):
                    break

        if found != len(auth_vars):
            return False

        auth_vars['cookies'] = '; '.join(['%s=%s' % (cookie.name, cookie.value)
                                          for cookie in cookiejar])

        config_path = self._get_config_path()

        f = open(config_path, 'w')
        f.write(json.dumps(auth_vars))
        f.close()

        self._load_config()

        return True

    def get_track_list(self, album):
        data = {
            'sortCriteriaList': '',
            'maxResults': album.num_tracks,
            'nextResultsToken': 0,
            'distinctOnly': 'false',
            'countOnly': 'false',
        }

        data.update(self._build_search_criteria(
            key_prefix='selectCriteriaList',
            search=[
                ('status', 'EQUALS', 'AVAILABLE'),
                ('trackStatus', 'IS_NULL'),
                ('sortAlbumArtistName', 'EQUALS', album.sort_album_artist_name),
                ('sortAlbumName', 'EQUALS', album.sort_album_name),
            ]))

        data.update(self._build_selected_columns(Song.COLUMNS))
        data.update(self._build_sort_criteria([
            ('discNum', 'ASC'),
            ('trackNum', 'ASC'),
        ]))

        result = self._get('selectTrackMetadata', data)
        items = self._get_payload_data(result, [
            'selectTrackMetadataResponse',
            'selectTrackMetadataResult',
            'trackInfoList',
        ])

        return [Song(self, item) for item in items]

    def get_song_stream_urls(self, song_ids):
        data = {}

        for i, song_id in enumerate(song_ids):
            data['trackIdList.member.%d' % (i + 1)] = song_id

        result = self._get('getStreamUrls', data)
        items = self._get_payload_data(result, [
            'getStreamUrlsResponse',
            'getStreamUrlsResult',
            'trackStreamUrlList',
        ])
        return [item['url'] for item in items]

    def get_songs(self,
                  search=[],
                  sort=DEFAULT_SONG_SORT,
                  *args, **kwargs):
        for song in self._search_library(return_type='TRACKS',
                                         result_cls=Song,
                                         result_key='songs',
                                         search=self.SONG_SEARCH + search,
                                         columns=Song.COLUMNS,
                                         sort=sort,
                                         *args, **kwargs):
            yield song

    def get_albums(self,
                   search=[],
                   sort=DEFAULT_ALBUM_SORT,
                   *args, **kwargs):
        for album in self._search_library(return_type='ALBUMS',
                                          result_cls=Album,
                                          result_key='albums',
                                          search=self.ALBUM_SEARCH + search,
                                          columns=Album.COLUMNS,
                                          sort=sort,
                                          *args, **kwargs):
            yield album

    def get_album(self, artist_name, album_name):
        results = list(self.get_albums(search=[
            ('artistName', 'EQUALS', artist_name),
            ('albumName', 'EQUALS', album_name),
        ]))

        if len(results) > 1:
            logging.error("get_album returned too many results for "
                          "artist '%s', album '%s'. Returning the first.",
                          artist_name, album_name)

        if results:
            return results[0]
        else:
            return None

    def get_artists(self,
                    search=[],
                    sort=DEFAULT_ARTIST_SORT,
                    *args, **kwargs):
        for artist in self._search_library(return_type='ARTISTS',
                                           result_cls=Artist,
                                           result_key='artists',
                                           search=self.ARTIST_SEARCH + search,
                                           columns=Artist.COLUMNS,
                                           sort=sort,
                                           *args, **kwargs):
            yield artist

    def _search_library(self, return_type, result_key, result_cls,
                        search=[], columns=[], sort=[]):
        next_results_token = ''
        i = 0

        while 1:
            data = {
                'searchReturnType': return_type,
                'albumArtUrlsSizeList.member.1': 'MEDIUM',
                'sortCriteriaList': '',
                'maxResults': self.PAGINATE_BY,
                'nextResultsToken': next_results_token,
            }

            data.update(self._build_search_criteria(search))
            data.update(self._build_selected_columns(columns))
            data.update(self._build_sort_criteria(sort))

            result = self._get('searchLibrary', data)

            search_results = self._get_payload_data(result, [
                'searchLibraryResponse',
                'searchLibraryResult',
            ])
            items = self._get_payload_data(search_results, [
                'searchReturnItemList',
            ])

            for item in items:
                yield result_cls(self, item)

            next_results_token = search_results['nextResultsToken']

            if not next_results_token:
                return

    def _get(self, operation, data):
        headers = {
            'x-amzn-RequestId': self._make_request_id(),
            'x-adp-token': self.adp_token,
            'x-RequestedWith': 'XMLHttpRequest',
            'User-Agent': self.USER_AGENT,
            'Referer': self.REFERER,
            'Origin': self.ORIGIN,
            'Cookie': self.cookies,
            'Host': 'www.amazon.com',
        }

        data.update({
            'Operation': operation,
            'ContentType': 'JSON',
            'customerInfo.customerId': self.customer_id,
            'customerInfo.deviceId': self.device_id,
            'customerInfo.deviceType': self.device_type,
        })

        r = requests.post(self.API_URL, data=data, headers=headers)
        result = r.json

        if r.status_code != 200:
            error = result['Error']
            raise RequestError(error['Message'],
                               error['Code'])

        return result

    def _load_config(self):
        config_path = self._get_config_path()

        if not os.path.exists(config_path):
            self.authenticated = False
            return

        f = open(config_path, 'r')
        config = json.loads(f.read())
        f.close()

        self.customer_id = config['customer_id']
        self.adp_token = config['adp_token']
        self.device_id = config['device_id']
        self.device_type = config['device_type']
        self.cookies = config['cookies']
        self.authenticated = True

    def _get_config_path(self):
        if 'APPDATA' in os.environ:
            homepath = os.environ['APPDATA']
        elif 'HOME' in os.environ:
            homepath = os.environ["HOME"]
        else:
            logging.warning('Unable to find home directory for '
                            '.cloudplayarc\n')
            homepath = ''

        return os.path.join(homepath, '.cloudplayarc')

    def _make_request_id(self):
        def get_rand():
            return hex(int(math.floor((1 + random.random()) * 65536)))[3:]

        return '%s%s-%s-dmcp-%s-%s%s%s' % (
            get_rand(),
            get_rand(),
            get_rand(),
            get_rand(),
            get_rand(),
            get_rand(),
            get_rand(),
        )

    def _get_payload_data(self, data, keys):
        for key in keys:
            if key not in data:
                raise RequestError('Missing key "%s" in response data' % key)

            data = data[key]

        return data

    def _build_search_criteria(self, search, key_prefix='searchCriteria'):
        data = {}

        for i, item in enumerate(search):
            key = '%s.member.%d' % (key_prefix, i + 1)
            data[key + '.attributeName'] = item[0]
            data[key + '.comparisonType'] = item[1]

            if len(item) == 2:
                value = ''
            else:
                value = item[2]

            data[key + '.attributeValue'] = value

        return data

    def _build_selected_columns(self, columns):
        data = {}

        for i, item in enumerate(columns):
            data['selectedColumns.member.%d' % (i + 1)] = item

        return data

    def _build_sort_criteria(self, sort):
        data = {}

        for i, item in enumerate(sort):
            key = 'sortCriteriaList.member.%d' % (i + 1)
            data[key + '.sortColumn'] = item[0]
            data[key + '.sortType'] = item[1]

        return data
