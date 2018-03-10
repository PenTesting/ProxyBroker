import asyncio
import re
import warnings
from math import sqrt
from html import unescape
from base64 import b64decode
from urllib.parse import unquote, urlparse

import aiohttp

import async_timeout

from .errors import BadStatusError
from .utils import log, get_headers, IPPattern, IPPortPatternGlobal


class Provider:
    """Proxy provider.

    Provider - a website that publish free public proxy lists.

    :param str url: Url of page where to find proxies
    :param tuple proto:
        (optional) List of the types (protocols) that may be supported
        by proxies returned by the provider. Then used as :attr:`Proxy.types`
    :param int max_conn:
        (optional) The maximum number of concurrent connections on the provider
    :param int max_tries:
        (optional) The maximum number of attempts to receive response
    :param int timeout:
        (optional) Timeout of a request in seconds
    """

    _pattern = IPPortPatternGlobal

    def __init__(self, url=None, proto=(), max_conn=4,
                 max_tries=3, timeout=20, loop=None):
        if url:
            self.domain = urlparse(url).netloc
        self.url = url
        self.proto = proto
        self._max_tries = max_tries
        self._timeout = timeout
        self._session = None
        self._cookies = {}
        self._proxies = set()
        # concurrent connections on the current provider
        self._sem_provider = asyncio.Semaphore(max_conn)
        self._loop = loop or asyncio.get_event_loop()

    @property
    def proxies(self):
        """Return all found proxies.

        :return:
            Set of tuples with proxy hosts, ports and types (protocols)
            that may be supported (from :attr:`.proto`).

            For example:
                {('192.168.0.1', '80', ('HTTP', 'HTTPS'), ...)}

        :rtype: set
        """
        return self._proxies

    @proxies.setter
    def proxies(self, new):
        new = [(host, port, self.proto) for host, port in new if port]
        self._proxies.update(new)

    async def get_proxies(self):
        """Receive proxies from the provider and return them.

        :return: :attr:`.proxies`
        """
        log.debug('Try to get proxies from %s' % self.domain)

        async with aiohttp.ClientSession(headers=get_headers(),
                                         cookies=self._cookies,
                                         loop=self._loop) as self._session:
            await self._pipe()

        log.debug('%d proxies received from %s: %s' % (
                  len(self.proxies), self.domain, self.proxies))
        return self.proxies

    async def _pipe(self):
        await self._find_on_page(self.url)

    async def _find_on_pages(self, urls):
        if not urls:
            return
        tasks = []
        if not isinstance(urls[0], dict):
            urls = set(urls)
        for url in urls:
            if isinstance(url, dict):
                tasks.append(self._find_on_page(**url))
            else:
                tasks.append(self._find_on_page(url))
        await asyncio.gather(*tasks)

    async def _find_on_page(self, url, data=None, headers=None, method='GET'):
        page = await self.get(url, data=data, headers=headers, method=method)
        oldcount = len(self.proxies)
        try:
            received = self.find_proxies(page)
        except Exception as e:
            received = []
            log.error('Error when executing find_proxies.'
                      'Domain: %s; Error: %r' % (self.domain, e))
        self.proxies = received
        added = len(self.proxies) - oldcount
        log.debug('%d(%d) proxies added(received) from %s' % (
            added, len(received), url))

    async def get(self, url, data=None, headers=None, method='GET'):
        for _ in range(self._max_tries):
            page = await self._get(
                url, data=data, headers=headers, method=method)
            if page:
                break
        return page

    async def _get(self, url, data=None, headers=None, method='GET'):
        page = ''
        try:
            with (await self._sem_provider),\
                    async_timeout.timeout(self._timeout, loop=self._loop):
                async with self._session.request(
                        method, url, data=data, headers=headers) as resp:
                    page = await resp.text()
                    if resp.status != 200:
                        log.debug(
                            'url: %s\nheaders: %s\ncookies: %s\npage:\n%s' % (
                                url, resp.headers, resp.cookies, page))
                        raise BadStatusError('Status: %s' % resp.status)
        except (UnicodeDecodeError, BadStatusError, asyncio.TimeoutError,
                aiohttp.ClientOSError, aiohttp.ClientResponseError,
                aiohttp.ServerDisconnectedError) as e:
            page = ''
            log.debug('%s is failed. Error: %r;' % (url, e))
        return page

    def find_proxies(self, page):
        return self._find_proxies(page)

    def _find_proxies(self, page):
        proxies = self._pattern.findall(page)
        return proxies


class Freeproxylists_com(Provider):
    domain = 'freeproxylists.com'

    async def _pipe(self):
        exp = r'''href\s*=\s*['"](?P<t>[^'"]*)/(?P<uts>\d{10})[^'"]*['"]'''
        urls = ['http://www.freeproxylists.com/socks.html',
                'http://www.freeproxylists.com/elite.html',
                'http://www.freeproxylists.com/anonymous.html']
        pages = await asyncio.gather(*[self.get(url) for url in urls])
        params = re.findall(exp, ''.join(pages))
        tpl = 'http://www.freeproxylists.com/load_{}_{}.html'
        # example: http://www.freeproxylists.com/load_socks_1448724717.html
        urls = [tpl.format(t, uts) for t, uts in params]
        await self._find_on_pages(urls)


class Blogspot_com_base(Provider):
    _cookies = {'NCR': 1}

    async def _pipe(self):
        exp = r'''<a href\s*=\s*['"]([^'"]*\.\w+/\d{4}/\d{2}/[^'"#]*)['"]>'''
        pages = await asyncio.gather(*[
            self.get('http://%s/' % d) for d in self.domains])
        urls = re.findall(exp, ''.join(pages))
        await self._find_on_pages(urls)


class Blogspot_com(Blogspot_com_base):
    domain = 'blogspot.com'
    domains = ['sslproxies24.blogspot.com', 'proxyserverlist-24.blogspot.com',
               'freeschoolproxy.blogspot.com', 'googleproxies24.blogspot.com']


class Blogspot_com_socks(Blogspot_com_base):
    domain = 'blogspot.com^socks'
    domains = ['www.socks24.org', ]


class Webanetlabs_net(Provider):
    domain = 'webanetlabs.net'

    async def _pipe(self):
        exp = r'''href\s*=\s*['"]([^'"]*proxylist_at_[^'"]*)['"]'''
        page = await self.get('https://webanetlabs.net/publ/24')
        urls = ['https://webanetlabs.net%s' % path
                for path in re.findall(exp, page)]
        await self._find_on_pages(urls)


class Checkerproxy_net(Provider):
    domain = 'checkerproxy.net'

    async def _pipe(self):
        exp = r'''href\s*=\s*['"](/archive/\d{4}-\d{2}-\d{2})['"]'''
        page = await self.get('https://checkerproxy.net/')
        urls = ['https://checkerproxy.net/api%s' % path
                for path in re.findall(exp, page)]
        await self._find_on_pages(urls)


class Proxz_com(Provider):
    domain = 'proxz.com'

    def find_proxies(self, page):
        return self._find_proxies(unquote(page))

    async def _pipe(self):
        exp = r'''href\s*=\s*['"]([^'"]?proxy_list_high_anonymous_[^'"]*)['"]'''  # noqa
        url = 'http://www.proxz.com/proxy_list_high_anonymous_0.html'
        page = await self.get(url)
        urls = ['http://www.proxz.com/%s' % path
                for path in re.findall(exp, page)]
        urls.append(url)
        await self._find_on_pages(urls)


class Proxy_list_org(Provider):
    domain = 'proxy-list.org'
    _pattern = re.compile(r'''Proxy\('([\w=]+)'\)''')

    def find_proxies(self, page):
        return [b64decode(hp).decode().split(':')
                for hp in self._find_proxies(page)]

    async def _pipe(self):
        exp = r'''href\s*=\s*['"]\./([^'"]?index\.php\?p=\d+[^'"]*)['"]'''
        url = 'http://proxy-list.org/english/index.php?p=1'
        page = await self.get(url)
        urls = ['http://proxy-list.org/english/%s' % path
                for path in re.findall(exp, page)]
        urls.append(url)
        await self._find_on_pages(urls)


class Aliveproxy_com(Provider):
    # more: http://www.aliveproxy.com/socks-list/socks5.aspx/United_States-us
    domain = 'aliveproxy.com'

    async def _pipe(self):
        paths = [
            'socks5-list', 'high-anonymity-proxy-list', 'anonymous-proxy-list',
            'fastest-proxies', 'us-proxy-list', 'gb-proxy-list',
            'fr-proxy-list', 'de-proxy-list', 'jp-proxy-list', 'ca-proxy-list',
            'ru-proxy-list', 'proxy-list-port-80', 'proxy-list-port-81',
            'proxy-list-port-3128', 'proxy-list-port-8000',
            'proxy-list-port-8080']
        urls = ['http://www.aliveproxy.com/%s/' % path for path in paths]
        await self._find_on_pages(urls)


# редиректит хуй поми кудаъ
class Maxiproxies_com(Provider):
    domain = 'maxiproxies.com'

    async def _pipe(self):
        exp = r'''<a href\s*=\s*['"]([^'"]*example[^'"#]*)['"]>'''
        page = await self.get('http://maxiproxies.com/category/proxy-lists/')
        urls = re.findall(exp, page)
        await self._find_on_pages(urls)


class _50kproxies_com(Provider):
    domain = '50kproxies.com'

    async def _pipe(self):
        exp = r'''<a href\s*=\s*['"]([^'"]*-proxy-list-[^'"#]*)['"]>'''
        page = await self.get('http://50kproxies.com/category/proxy-list/')
        urls = re.findall(exp, page)
        await self._find_on_pages(urls)


class Proxylist_me(Provider):
    domain = 'proxylist.me'

    async def _pipe(self):
        exp = r'''href\s*=\s*['"][^'"]*/?page=(\d+)['"]'''
        page = await self.get('https://proxylist.me/')
        lastId = max([int(n) for n in re.findall(exp, page)])
        urls = ['https://proxylist.me/?page=%d' % n for n in range(lastId)]
        await self._find_on_pages(urls)


class Foxtools_ru(Provider):
    domain = 'foxtools.ru'

    async def _pipe(self):
        urls = ['http://api.foxtools.ru/v2/Proxy.txt?page=%d' % n
                for n in range(1, 6)]
        await self._find_on_pages(urls)


class Gatherproxy_com(Provider):
    domain = 'gatherproxy.com'
    _pattern_h = re.compile(
        r'''(?P<ip>(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?))'''  # noqa
        r'''(?=.*?(?:(?:(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?))|'(?P<port>[\d\w]+)'))''',  # noqa
        flags=re.DOTALL)

    def find_proxies(self, page):
        # if 'gp.dep' in page:
        #     proxies = self._pattern_h.findall(page)  # for http(s)
        #     proxies = [(host, str(int(port, 16)))
        #                for host, port in proxies if port]
        # else:
        #     proxies = self._find_proxies(page)  # for socks
        return [(host, str(int(port, 16)))
                for host, port in self._pattern_h.findall(page) if port]

    async def _pipe(self):
        url = 'http://www.gatherproxy.com/proxylist/anonymity/'
        expNumPages = r'href="#(\d+)"'
        method = 'POST'
        # hdrs = {'Content-Type': 'application/x-www-form-urlencoded'}
        urls = []
        for t in ['anonymous', 'elite']:
            data = {'Type': t, 'PageIdx': 1}
            page = await self.get(url, data=data, method=method)
            if not page:
                continue
            lastPageId = max([int(n) for n in re.findall(expNumPages, page)])
            urls = [{'url': url, 'data': {'Type': t, 'PageIdx': pid},
                     'method': method} for pid in range(1, lastPageId + 1)]
        # urls.append({'url': 'http://www.gatherproxy.com/sockslist/',
        #              'method': method})
        await self._find_on_pages(urls)


class Gatherproxy_com_socks(Provider):
    domain = 'gatherproxy.com^socks'

    async def _pipe(self):
        urls = [{'url': 'http://www.gatherproxy.com/sockslist/',
                 'method': 'POST'}]
        await self._find_on_pages(urls)


class Tools_rosinstrument_com_base(Provider):
    # more: http://tools.rosinstrument.com/cgi-bin/
    #       sps.pl?pattern=month-1&max=50&nskip=0&file=proxlog.csv
    domain = 'tools.rosinstrument.com'
    sqrtPattern = re.compile(r'''sqrt\((\d+)\)''')
    bodyPattern = re.compile(r'''hideTxt\(\n*'(.*)'\);''')
    _pattern = re.compile(
        r'''(?:(?P<domainOrIP>(?:[a-z0-9\-.]+\.[a-z]{2,6})|'''
        r'''(?:(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}'''
        r'''(?:25[0-5]|2[0-4]\d|[01]?\d\d?))))(?=.*?(?:(?:'''
        r'''[a-z0-9\-.]+\.[a-z]{2,6})|(?:(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)'''
        r'''\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?))|(?P<port>\d{2,5})))''',
        flags=re.DOTALL)

    def find_proxies(self, page):
        x = self.sqrtPattern.findall(page)
        if not x:
            return []
        x = round(sqrt(float(x[0])))
        hiddenBody = self.bodyPattern.findall(page)[0]
        hiddenBody = unquote(hiddenBody)
        toCharCodes = [ord(char) ^ (x if i % 2 else 0)
                       for i, char in enumerate(hiddenBody)]
        fromCharCodes = ''.join([chr(n) for n in toCharCodes])
        page = unescape(fromCharCodes)
        return self._find_proxies(page)


class Tools_rosinstrument_com(Tools_rosinstrument_com_base):
    domain = 'tools.rosinstrument.com'

    async def _pipe(self):
        tpl = 'http://tools.rosinstrument.com/raw_free_db.htm?%d&t=%d'
        urls = [tpl % (pid, t) for pid in range(51) for t in range(1, 3)]
        await self._find_on_pages(urls)


class Tools_rosinstrument_com_socks(Tools_rosinstrument_com_base):
    domain = 'tools.rosinstrument.com^socks'

    async def _pipe(self):
        tpl = 'http://tools.rosinstrument.com/raw_free_db.htm?%d&t=3'
        urls = [tpl % pid for pid in range(51)]
        await self._find_on_pages(urls)


class Xseo_in(Provider):
    domain = 'xseo.in'
    charEqNum = {}

    def char_js_port_to_num(self, matchobj):
        chars = matchobj.groups()[0]
        num = ''.join([self.charEqNum[ch] for ch in chars if ch != '+'])
        return num

    def find_proxies(self, page):
        expPortOnJS = r'\(""\+(?P<chars>[a-z+]+)\)'
        expCharNum = r'\b(?P<char>[a-z])=(?P<num>\d);'
        self.charEqNum = {char: i for char, i in re.findall(expCharNum, page)}
        page = re.sub(expPortOnJS, self.char_js_port_to_num, page)
        return self._find_proxies(page)

    async def _pipe(self):
        await self._find_on_page(
            url='http://xseo.in/proxylist', data={'submit': 1}, method='POST')


class Nntime_com(Provider):
    domain = 'nntime.com'
    charEqNum = {}
    _pattern = re.compile(
        r'''\b(?P<ip>(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}'''
        r'''(?:25[0-5]|2[0-4]\d|[01]?\d\d?))(?=.*?(?:(?:(?:(?:25'''
        r'''[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)'''
        r''')|(?P<port>\d{2,5})))''',
        flags=re.DOTALL)

    def char_js_port_to_num(self, matchobj):
        chars = matchobj.groups()[0]
        num = ''.join([self.charEqNum[ch] for ch in chars if ch != '+'])
        return num

    def find_proxies(self, page):
        expPortOnJS = r'\(":"\+(?P<chars>[a-z+]+)\)'
        expCharNum = r'\b(?P<char>[a-z])=(?P<num>\d);'
        self.charEqNum = {char: i for char, i in re.findall(expCharNum, page)}
        page = re.sub(expPortOnJS, self.char_js_port_to_num, page)
        return self._find_proxies(page)

    async def _pipe(self):
        tpl = 'http://www.nntime.com/proxy-updated-{:02}.htm'
        urls = [tpl.format(n) for n in range(1, 31)]
        await self._find_on_pages(urls)


class Proxynova_com(Provider):
    domain = 'proxynova.com'

    async def _pipe(self):
        expCountries = r'"([a-z]{2})"'
        page = await self.get('https://www.proxynova.com/proxy-server-list/')
        tpl = 'https://www.proxynova.com/proxy-server-list/country-%s/'
        urls = [tpl % isoCode for isoCode in re.findall(expCountries, page)
                if isoCode != 'en']
        await self._find_on_pages(urls)


class Spys_ru(Provider):
    domain = 'spys.ru'
    charEqNum = {}

    def char_js_port_to_num(self, matchobj):
        chars = matchobj.groups()[0].split('+')
        # ex: '+(i9w3m3^k1y5)+(g7g7g7^v2e5)+(d4r8o5^i9u1)+(y5c3e5^t0z6)'
        # => ['', '(i9w3m3^k1y5)', '(g7g7g7^v2e5)',
        #     '(d4r8o5^i9u1)', '(y5c3e5^t0z6)']
        # => ['i9w3m3', 'k1y5'] => int^int
        num = ''
        for numOfChars in chars[1:]:  # first - is ''
            var1, var2 = numOfChars.strip('()').split('^')
            digit = self.charEqNum[var1] ^ self.charEqNum[var2]
            num += str(digit)
        return num

    def find_proxies(self, page):
        expPortOnJS = r'(?P<js_port_code>(?:\+\([a-z0-9^+]+\))+)'
        # expCharNum = r'\b(?P<char>[a-z\d]+)=(?P<num>[a-z\d\^]+);'
        expCharNum = r'[>;]{1}(?P<char>[a-z\d]{4,})=(?P<num>[a-z\d\^]+)'
        # self.charEqNum = {
        #     char: i for char, i in re.findall(expCharNum, page)}
        res = re.findall(expCharNum, page)
        for char, num in res:
            if '^' in num:
                digit, tochar = num.split('^')
                num = int(digit) ^ self.charEqNum[tochar]
            self.charEqNum[char] = int(num)
        page = re.sub(expPortOnJS, self.char_js_port_to_num, page)
        return self._find_proxies(page)

    async def _pipe(self):
        expSession = r"'([a-z0-9]{32})'"
        url = 'http://spys.one/proxies/'
        page = await self.get(url)
        sessionId = re.findall(expSession, page)[0]
        data = {'xf0': sessionId,  # session id
                'xpp': 3,          # 3 - 200 proxies on page
                'xf1': None}       # 1 = ANM & HIA; 3 = ANM; 4 = HIA
        method = 'POST'
        urls = [{'url': url, 'data': {**data, 'xf1': lvl},
                 'method': method} for lvl in [3, 4]]
        await self._find_on_pages(urls)
        # expCountries = r'>([A-Z]{2})<'
        # url = 'http://spys.ru/proxys/'
        # page = await self.get(url)
        # links = ['http://spys.ru/proxys/%s/' %
        #          isoCode for isoCode in re.findall(expCountries, page)]


class My_proxy_com(Provider):
    domain = 'my-proxy.com'

    async def _pipe(self):
        exp = r'''href\s*=\s*['"]([^'"]?free-[^'"]*)['"]'''
        url = 'https://www.my-proxy.com/free-proxy-list.html'
        page = await self.get(url)
        urls = ['https://www.my-proxy.com/%s' % path
                for path in re.findall(exp, page)]
        urls.append(url)
        await self._find_on_pages(urls)


class Free_proxy_cz(Provider):
    domain = 'free-proxy.cz'
    _pattern = re.compile(
        r'''decode\("([\w=]+)".*?\("([\w=]+)"\)''', flags=re.DOTALL)

    def find_proxies(self, page):
        return [(b64decode(h).decode(), b64decode(p).decode())
                for h, p in self._find_proxies(page)]

    async def _pipe(self):
        tpl = 'http://free-proxy.cz/en/proxylist/main/date/%d'
        urls = [tpl % n for n in range(1, 15)]
        await self._find_on_pages(urls)
        # _urls = []
        # for url in urls:
        #     if len(_urls) == 15:
        #         await self._find_on_pages(_urls)
        #         print('sleeping on 61 sec')
        #         await asyncio.sleep(61)
        #         _urls = []
        #     _urls.append(url)
        # =========
        # expNumPages = r'href="/en/proxylist/main/(\d+)"'
        # page = await self.get('http://free-proxy.cz/en/')
        # if not page:
        #     return
        # lastPageId = max([int(n) for n in re.findall(expNumPages, page)])
        # tpl = 'http://free-proxy.cz/en/proxylist/main/date/%d'
        # urls = [tpl % pid for pid in range(1, lastPageId+1)]
        # _urls = []
        # for url in urls:
        #     if len(_urls) == 15:
        #         await self._find_on_pages(_urls)
        #         print('sleeping on 61 sec')
        #         await asyncio.sleep(61)
        #         _urls = []
        #     _urls.append(url)


class Proxyb_net(Provider):
    domain = 'proxyb.net'
    _port_pattern_b64 = re.compile(r"stats\('([\w=]+)'\)")
    _port_pattern = re.compile(r"':(\d+)'")

    def find_proxies(self, page):
        if not page:
            return []
        _hosts, _ports = page.split('","ports":"')
        hosts, ports = [], []
        for host in _hosts.split('<\/tr><tr>'):
            host = IPPattern.findall(host)
            if not host:
                continue
            hosts.append(host[0])
        ports = [self._port_pattern.findall(b64decode(port).decode())[0]
                 for port in self._port_pattern_b64.findall(_ports)]
        return [(host, port) for host, port in zip(hosts, ports)]

    async def _pipe(self):
        url = 'http://proxyb.net/ajax.php'
        method = 'POST'
        data = {'action': 'getProxy', 'p': 0,
                'page': '/anonimnye_proksi_besplatno.html'}
        hdrs = {'X-Requested-With': 'XMLHttpRequest'}
        urls = [{'url': url, 'data': {**data, 'p': p},
                 'method': method, 'headers': hdrs} for p in range(0, 151)]
        await self._find_on_pages(urls)


class Proxylistplus_com(Provider):
    domain = 'list.proxylistplus.com'

    async def _pipe(self):
        names = ['Fresh-HTTP-Proxy', 'SSL', 'Socks']
        urls = ['http://list.proxylistplus.com/%s-List-%d' % (i, n)
                for i in names for n in range(1, 7)]
        await self._find_on_pages(urls)


class ProxyProvider(Provider):
    def __init__(self, *args, **kwargs):
        warnings.warn('`ProxyProvider` is deprecated, use `Provider` instead.',
                      DeprecationWarning)
        super().__init__(*args, **kwargs)


PROVIDERS = [
    Provider(url='http://www.proxylists.net/',
             proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25')),  # 49
    Provider(url='http://ipaddress.com/proxy-list/',
             proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25')),  # 53
    Provider(url='https://www.sslproxies.org/',
             proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25')),  # 100
    Provider(url='https://freshfreeproxylist.wordpress.com/',
             proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25')),  # 50
    Provider(url='http://proxytime.ru/http',
             proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25')),  # 1400
    Provider(url='https://free-proxy-list.net/',
             proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25')),  # 300
    Provider(url='https://us-proxy.org/',
             proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25')),  # 200
    Provider(url='http://fineproxy.org/eng/fresh-proxies/',
             proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25')),  # 5500
    Provider(url='https://socks-proxy.net/',
             proto=('SOCKS4', 'SOCKS5')),                           # 80
    Provider(url='http://www.httptunnel.ge/ProxyListForFree.aspx',
             proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25')),  # 200
    Provider(url='http://cn-proxy.com/',
             proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25')),  # 70
    Provider(url='https://hugeproxies.com/home/',
             proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25')),  # 800
    Provider(url='http://proxy.rufey.ru/',
             proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25')),  # 153
    Provider(url='https://geekelectronics.org/my-servisy/proxy',
             proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25')),  # 400
    Provider(url='http://pubproxy.com/api/proxy?limit=20&format=txt',
             proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25')),  # 20
    Proxy_list_org(proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25')),           # noqa; 140
    Xseo_in(proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25')),                  # noqa; 240
    Spys_ru(proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25')),                  # noqa; 660
    Proxylistplus_com(proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25')),        # noqa; 450
    Proxylist_me(proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25')),             # noqa; 2872
    Foxtools_ru(proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25'), max_conn=1),  # noqa; 500
    Gatherproxy_com(proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25')),          # noqa; 3212
    Nntime_com(proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25')),               # noqa; 1050
    Blogspot_com(proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25')),             # noqa; 24800
    Gatherproxy_com_socks(proto=('SOCKS4', 'SOCKS5')),                             # noqa; 30
    Blogspot_com_socks(proto=('SOCKS4', 'SOCKS5')),                                # noqa; 1486
    Tools_rosinstrument_com(proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25')),  # noqa; 4000
    Tools_rosinstrument_com_socks(proto=('SOCKS4', 'SOCKS5')),                     # noqa; 1800
    My_proxy_com(max_conn=2),                                                      # noqa; 1000
    Checkerproxy_net(),                                                            # noqa; 60000
    Aliveproxy_com(),                                                              # noqa; 210
    Freeproxylists_com(),                                                          # noqa; 1338
    Webanetlabs_net(),                                                             # noqa; 5000
    Maxiproxies_com(),                                                             # noqa; 430
    sslproxies24_top(),
    samair_ru(),
    proxz_com(),
    ttvnol_com(),
    aliveproxy_com(),
    proxylisty_com(),
    proxylists_net(),
    proxiesfree_wordpress_com(),
    gfgdailyproxies_blogspot_com(),
    proxies4net_blogspot_com(),
    grabberz_com(),
    proxysmack_com(),
    proxyfire_net(),
    getproxy_jp(),
    captchapanel_blogspot_com(),
    proxieslist_todownload_net(),
    topproxies4_blogspot_com(),
    service_freelanceronline_ru(),
    crackcommunity_com(),
    kemoceng_com(),
    xroxy_com(),
    zismo_biz(),
    dailyprox_blogspot_com(),
    mobilite_waptools_net(),
    mutlulukkenti_com(),
    my-proxy_com(),
    blast_hk(),
    idproxy_blogspot_com(),
    golden-proxy-download-free_blogspot_com(),
    tomoney_narod_ru(),
    proxy-free-liste_blogspot_com(),
    socks5heaven_blogspot_com(),
    dheart_net(),
    therealist_ru(),
    wangolds_com(),
    yun-daili_com(),
    proxy-faq_de(),
    socks24_org(),
    goldsocks_blogspot_com(),
    best-proxy_com(),
    base_4rumer_com(),
    ailevadisi_net(),
    jdm-proxies_blogspot_com(),
    imanhost_in(),
    plsn_com_websitedetective_net(),
    us-proxy-server-list_blogspot_com(),
    members_tripod_com(),
    forum_freeproxy_ru(),
    dogdev_net(),
    ss_ciechanowski_org(),
    github_com(),
    cc-ppfresh_blogspot_com(),
    bestallproxy_blogspot_com(),
    happy-proxy_com(),
    web-proxy-list_blogspot_com(),
    blog_fzcnjp_com(),
    blackhatworld_com(),
    proxy-free-list_blogspot_com(),
    findipaddress_info(),
    hackers-cafe_com(),
    popularasians_com(),
    ip-adress_com(),
    proxyvadi_net(),
    freshdailyproxyblogspot_blogspot_com(),
    shyronium_com(),
    updatefreeproxylist_blogspot_com(),
    csm_cloudbehind_com(),
    proreload_blogspot_com(),
    socks5_wz-dns_net(),
    check-proxylist_blogspot_com(),
    atomintersoft_com(),
    dronsoftwares_blogspot_com(),
    maxasoft_bplaced_net(),
    d3scene_ru(),
    punjabi-cheetay_blogspot_com(),
    proxylistchecker_org(),
    pro_xxq_ru(),
    sslproxies24_blogspot_com(),
    buondon_info(),
    brainviewhackers_blogspot_com(),
    dome-project_net(),
    ultrasurf_org(),
    proxiytoday_blogspot_com(),
    proxyeasy_tk(),
    raw_githubusercontent_com(),
    yysyuan_com(),
    ninjaos_org(),
    devil-socks_blogspot_com(),
    free-proxy-indonesia_blogspot_com(),
    proxy_speedtest_at(),
    netforumlari_com(),
    nntime_com(),
    socks-proxy_net(),
    nimueh_ru(),
    proxylist_j1f_net(),
    proxyskull_blogspot_com(),
    ye-mao_info(),
    baglanforum_10tr_net(),
    platniy-nomer_blogspot_com(),
    priv8-tools_net(),
    lpccoder_blogspot_com(),
    pakunlock_blogspot_com(),
    darmoweproxy_blogspot_com(),
    premiumaccount_ubiktx_com(),
    rosinstrument_com(),
    alldayproxy_blogspot_com(),
    proksik_ru(),
    proxy-magnet_com(),
    hepgel_com(),
    rucrime_top(),
    free-proxy-lists_blogspot_com(),
    thebigproxylist_com(),
    proxyserverlist-update_blogspot_com(),
    seogrizz_edl_pl(),
    myproxylists_com(),
    pinoygizmos_com(),
    zhyk_ru(),
    rootjazz_com(),
    vip1_baizhongsou_com(),
    proxy-base_com(),
    hireaton_com(),
    game-monitor_com(),
    proxies_my-proxy_com(),
    freeglobalproxy_com(),
    proxy24update_blogspot_com(),
    scrapeboxproxylist_blogspot_com(),
    money8686_blogspot_com(),
    cfud_biz(),
    idcmz_com(),
    robotword_blogspot_com(),
    f16_h1_ru(),
    mikhed_narod_ru(),
    sbbtesting_com(),
    forum_anonymousvn_org(),
    astra_bbbv_ru(),
    getfreeproxy_com(),
    myip_net(),
    list_proxylistplus_com(),
    hacking-kesehatan_blogspot_de(),
    programslist_com(),
    proxylistsdaily_blogspot_com(),
    best-proxy_ru(),
    aliveproxies_com(),
    free-proxy-world_blogspot_com(),
    proxieslovers_blogspot_com(),
    testpzone_blogspot_com(),
    freesocksproxylist_blogspot_com(),
    cybersyndrome_net(),
    community_aliveproxy_com(),
    uk-proxy-server_blogspot_com(),
    proxy_ipcn_org(),
    mepe3_wordpress_com(),
    sirinlerim_org(),
    freesshsocksfresh_blogspot_com(),
    forum_safranboluforum_com(),
    wlnyshacker_wordpress_com(),
    cpmuniversal_blogspot_com(),
    fakeip_ru(),
    feedbot_cba_pl(),
    setting-jaringan_blogspot_com(),
    bulkmoneyforum_com(),
    etui_net_cn(),
    proxy-heaven_blogspot_com(),
    idssh_blogspot_com(),
    sadturtle_wordpress_com(),
    blog_bafoed_ru(),
    xn--j1ahceh8f_xn--p1ai(),
    socks404_blogspot_com(),
    vip-socks-bots-mg_blogspot_com(),
    getproxyblog_blogspot_com(),
    darkteam_net(),
    forum_sa3eka_com(),
    void_ru(),
    mocosoftx_com(),
    proxy79_blogspot_com(),
    free919_com_ar(),
    sockspremium_blogspot_com(),
    best-hacker_ru(),
    amusetech_net(),
    zillionere_com(),
    ranssh_blogspot_com(),
    turkdigital_gen_tr(),
    gatherproxy_com(),
    haoip_cc(),
    forumharika_com(),
    ies_html(),
    greenrain_bos_ru(),
    socks5proxies_com(),
    trickforums_net(),
    premiumofferclub_blogspot_com(),
    backtrack-group_blogspot_com(),
    sockproxy_blogspot_com(),
    proxy124_com(),
    eylulsohbet_com(),
    proxyserver3lite_blogspot_com(),
    proxy4ever_com(),
    proxiesdaily_pw(),
    shadow-mine_blogspot_com(),
    freedailyproxylist_blogspot_com(),
    thelotter_pro(),
    socks5updatefree_blogspot_com(),
    devilishproxies_wordpress_com(),
    linuxland_itam_nsc_ru(),
    compressportal_blogspot_com(),
    jetkingjbp_ucoz_com(),
    abu8_8m_com(),
    socks-prime_com(),
    urlhq1_com(),
    wiki_hidemyass_com(),
    proxyv_blogspot_com(),
    zrobisz-sam_blogspot_com(),
    gauchohack_8k_com(),
    sendbad_net(),
    gratis-ajib_blogspot_com(),
    ppmedia_dk(),
    proxyorsocks_blogspot_com(),
    monster-hack_su(),
    linuxplanet_org(),
    onlineclasses234_blogspot_com(),
    proxy-free-list_ru(),
    files_wiiaam_com(),
    my-list-proxies_blogspot_com(),
    epstur_wordpress_com(),
    blacked_in(),
    proxydumps_blogspot_com(),
    proxy_hosted-in_eu(),
    hackingballz_com(),
    proxysearcher_sourceforge_net(),
    masquersonip_com(),
    free-ip-proxyproxy_blogspot_com(),
    othersrvr_com(),
    dailizhijia_cn(),
    edirect-links_html(),
    red-proxy_blogspot_com(),
    youhack_ru(),
    roxy_globoxhost_com(),
    searchlores_org(),
    proxyswitcheroo_com(),
    kuaidaili_com(),
    skill-gamer_ru(),
    network_iwarp_com(),
    seoblackhatz_com(),
    paidseo_net(),
    agarproxy_tk(),
    pr0xysockslist_blogspot_com(),
    vipprox_blogspot_com(),
    notan_h1_ru(),
    psiphonhandler_blogspot_com(),
    listadeproxis_blogspot_com(),
    wapland_org(),
    proxyfire_tumblr_com(),
    freegoldenproxydownload_blogspot_com(),
    silamsohbet_net(),
    techonce_net(),
    ultraproxy_blogspot_com(),
    proxaro_blogspot_com(),
    proxy-base_info(),
    xproxy_blogspot_com(),
    wvs_io(),
    trendtv_website(),
    proxy_goubanjia_com(),
    freshproxie4u_blogspot_com(),
    rusproxy_blogspot_com(),
    socksnew_com(),
    admuncher_com(),
    checkerproxy_net(),
    forumcafe_org(),
    fineproxy_ru(),
    world34_blogspot_com(),
    gitu_me(),
    sshandsocks_blogspot_com(),
    proxylist2008_blogspot_com(),
    l2adr_com(),
    proxyblind_org(),
    cysbox7_blog_163_com(),
    wpu_platechno_com(),
    wisenet_ws(),
    search_yahoo_com(),
    phreaker56_xyz(),
    gavsi-sani_blogspot_com(),
    free-fresh-anonymous-proxies_blogspot_com(),
    feeds_feedburner_com(),
    swei360_com(),
    geoproxy_blogspot_com(),
    incloak_com(),
    mdn-ankteam_blogspot_com(),
    leakforcash_blogspot_com(),
    computerinitiate_blogspot_com(),
    proxynsocks_blogspot_com(),
    proxyserverlist_blogspot_com(),
    chemip_net(),
    peoplepages_chat_ru(),
    all-proxy_ru(),
    seoadvertisings_com(),
    proxies-unlimited_blogspot_com(),
    pr0xytofree_blogspot_com(),
    pps_unla_ac_id(),
    freeupdateproxyserver_blogspot_com(),
    favoriforumum_net(),
    pr0xy_me(),
    mexicoproxy_blogspot_com(),
    olaygazeteci_com(),
    freefreshproxy4u_blogspot_com(),
    freesocks24_blogspot_com(),
    free-proxy4u_blogspot_com(),
    daili_iphai_com(),
    -a_html(),
    stat_sven_ru(),
    ingvarr_net_ru(),
    socks24_ru(),
    kinosector_ru(),
    hidemyassproxies_blogspot_com(),
    grenxparta_blogspot_com(),
    amega_blogfree_net(),
    xls_su(),
    wmasteru_org(),
    free-socks5_ga(),
    anondwahyu_blogspot_com(),
    hotproxylist_blogspot_com(),
    chk_gblknjng_com(),
    ipaddress_com(),
    freeproxy_ch(),
    browseanonymously_net(),
    guncelproxy_tk(),
    socksv9_blogspot_com(),
    ingin-tau_ga(),
    lista-proxy_blogspot_com(),
    ssh-dailyupdate_blogspot_com(),
    www5_ocn_ne_jp(),
    urlquery_net(),
    the-proxy-list_com(),
    xsdaili_com(),
    fastusproxies_tk(),
    httproxy_blogspot_com(),
    fbtrick_blogspot_com(),
    proxybase_de(),
    proxysgerfull_blogspot_com(),
    advanceauto-show_blogspot_com(),
    blog_sohbetgo_com(),
    proxylistsdownload_blogspot_com(),
    mgvrk_by(),
    proxyocean_blogspot_com(),
    getproxy_net(),
    skypegrab_net(),
    proxyipchecker_com(),
    brazilproxies_blogspot_com(),
    dailyfreefreshproxies_wordpress_com(),
    mixday_net(),
    seoproxies_blogspot_com(),
    bacasimpel_blogspot_com(),
    tristat_org(),
    proxylist_ucoz_com(),
    warez-home_net(),
    ip84_com(),
    cyberhackers_org(),
    it892_com(),
    emoney_al_ru(),
    hack-faq_ru(),
    pirate-bay_in(),
    vipproxy_blogspot_com(),
    dnfastproxy_blogspot_com(),
    proxyleaks_blogspot_com(),
    turkirc_com(),
    usuarios_lycos_es(),
    final4ever_com(),
    premiumyogi_blogspot_com(),
    anon-hackers_forumfree_it(),
    liusasa_com(),
    shroomery_org(),
    high-anonymity-proxy-server-list_blogspot_com(),
    socks5proxys_blogspot_com(),
    rebro_weebly_com(),
    mysterecorp_com(),
    governmentsecurity_org(),
    xici_net(),
    stackoverflow_com(),
    proxyfirenet_blogspot_com(),
    proxy-level_blogspot_com(),
    us-proxy-server_blogspot_com(),
    sshshare_com(),
    download-files18_fo_ru(),
    freepremiumproxy_blogspot_com(),
    fighttracker_ru(),
    kantsuu_com(),
    goodproxies_wordpress_com(),
    yourproxies_blogspot_com(),
    simon-vlog_com(),
    yaxonto_narod_ru(),
    dayproxies_blogspot_com(),
    enterpass_vn(),
    webcheckproxy_blogspot_com(),
    margakencana_tk(),
    ninjaseotools_com(),
    irc-proxies24_blogspot_com(),
    huaci_net(),
    pirate_bz(),
    free-proxy-list_net(),
    proxypremium_blogspot_com(),
    proxy-socks_blogspot_com(),
    proxy001_blogspot_com(),
    free-vip-proxy_blogspot_com(),
    vibiz_net(),
    yusuthad_blogspot_com(),
    proxyape_com(),
    sensalgo_com(),
    nohatworld_com(),
    hsproxyip_edicy_co(),
    proxy-updated_blogspot_com(),
    jurnalproxies_blogspot_com(),
    sohbetruzgari_net(),
    googlepassedproxies_com(),
    mianfeipao_com(),
    proxy80elite_blogspot_com(),
    craktricks_blogspot_com(),
    cheap-flights-to-dubai_blogspot_com(),
    guncelproxyozel_blogcu_com(),
    socks5listblogspot_blogspot_com(),
    torvpn_com(),
    pass_efmsoft_com(),
    sslproxies_org(),
    gist_github_com(),
    livesproxy_com(),
    furious_freedom-vrn_ru(),
    chaojirj_com(),
    goobast_blogspot_com(),
    proxyout_net(),
    highspeedfreeproxylists_blogspot_com(),
    beatemailmass9947_wordpress_com(),
    trfrm_net(),
    httptunnel_ge(),
    proxy46_com(),
    proxy-ip-list_com(),
    proxyfire_tr_gg(),
    itzone_vn(),
    hungfap_blogspot_com(),
    ravizabagus_blogspot_com(),
    spys_ru(),
    atesclup_com(),
    mimiip_com(),
    sockproxyus_blogspot_com(),
    hack-hack_chat_ru(),
    pub365_blogspot_de(),
    clientn_platinumhideip_com(),
    golden-joint_com(),
    burningx_com(),
    forum_4rgameprivate_com(),
    hackthedemon_blogspot_com(),
    vipsock5us_blogspot_com(),
    icecms_cn(),
    proxyranker_com(),
    blackhatforum_co(),
    mircstar_blogcu_com(),
    egproxy_com(),
    cool-proxy_net(),
    get-proxy_ru(),
    wendang_docsou_com(),
    proxy-ip_cn(),
    free2y_blogspot_com(),
    go4free_xyz(),
    kid-winner_blogspot_com(),
    sslproxies24_blogspot_in(),
    notfound_ga(),
    vipiu_net(),
    live-socks_net(),
    webcr_narod_ru(),
    clientn_easy-hideip_com(),
    westdollar_narod_ru(),
    asifameerbakhsh_blogspot_com(),
    teri_2ch_net(),
    proxyapi_cf(),
    pypi_python_org(),
    tool_cccyun_cn(),
    thedarkcrypter_blogspot_com(),
    elite-proxy_ru(),
    dostifun_com(),
    proxyarl_blogspot_com(),
    kderoz_blogspot_com(),
    spoofs_de(),
    mytargets_ru(),
    globaltrickz_blogspot_com(),
    premiumproxylistupdates_blogspot_com(),
    proxybonanza_blogspot_com(),
    lutfifrastiko_blogspot_de(),
    xseo_in(),
    proxylistdownloads_blogspot_com(),
    extremetracking_com(),
    trojanforge-downloads_blogspot_com(),
    freeproxies4crawlers_blogspot_com(),
    scream-group_com(),
    qz321_net(),
    nafeestechtips_blogspot_com(),
    forum-hack-games-vk_ru(),
    oyunbox_10tl_net(),
    hung-thinh_blogspot_com(),
    bworm_vv_si(),
    cnproxy_com(),
    newgooddaily_blogspot_com(),
    gratisinjeksshvpn_blogspot_com(),
    livetvstreaminggado2_blogspot_com(),
    elite_spb_ru(),
    pingrui_net(),
    agarboter_ga(),
    gohhg_com(),
    nonijoho_blogspot_com(),
    ab57_ru(),
    socks5list_net(),
    proxydb_ru(),
    lopaskacreative18_blogspot_com(),
    xcarding_ru(),
    rdsoftword_blogspot_com(),
    dailyproxylists_quick-hide-ip_com(),
    mrhinkydink_com(),
    freeproxyus_blogspot_com(),
    pr0xies_org(),
    accforfree_blogspot_com(),
    wickedsamurai_org(),
    proxyhenven_blogspot_com(),
    proxyserverspecial_blogspot_com(),
    vipsocks24_com(),
    tero_space(),
    bhfreeproxies_blogspot_com(),
    fastsockproxy_blogspot_com(),
    prosocks24_blogspot_com(),
    trafficplanet_com(),
    proxyfree3_blogspot_com(),
    freedailyproxylistworking_blogspot_com(),
    cool-proxy_ru(),
    ebooksgenius_com(),
    proxy-listing_com(),
    prxfree_tumblr_com(),
    socksproxylists_blogspot_com(),
    meilleurvpn_net(),
    your-freedombrowsing_blogspot_com(),
    carderparadise_com(),
    melfori_com(),
    textproxylists_com(),
    leakmafia_com(),
    sohbetcisin_net(),
    leaksden_com(),
    cdjp_org(),
    megaproxy_blogspot_com(),
    new-proxy_blogspot_com(),
    avto-alberto_avtostar_si(),
    proxydaily_co_vu(),
    freeproxyaday_blogspot_com(),
    freeproxyserverslistuk_blogspot_com(),
    google-proxys_blogspot_com(),
    forumvk_com(),
    dexiz_ru(),
    proxyfree_wordpress_com(),
    dollar2000_chat_ru(),
    proxybag_blogspot_com(),
    clientn_superhideip_com(),
    proxy-hunter_blogspot_com(),
    proxy2u_blogspot_com(),
    dmfn_me(),
    proxynova_com(),
    mediabolism_com(),
    aa8_narod_ru(),
    cyozone_blogspot_com(),
    roxyproxy_me(),
    in4collect_blogspot_com(),
    yahoomblog_blogspot_com(),
    en_bablomet_org(),
    linkbucksanadfly_blogspot_com(),
    eliteproxyserverlist_blogspot_com(),
    torrentsafe_weebly_com(),
    koreaproxies_blogspot_com(),
    ip_2kr_kr(),
    trik_us(),
    personal_primorye_ru(),
    imoveismanaus-e_com_br(),
    miped_ru(),
    fm_elasa_ir(),
    kakushirazi_com(),
    freeproxy-bluesonicboy_blogspot_com(),
    intware-smma_blogspot_com(),
    free-proxy_blogspot_com(),
    free-proxy-list_ru(),
    proxy_web-box_ru(),
    service4bit_com(),
    bigdaili_com(),
    www6_tok2_com(),
    idonew_com(),
    crackingdrift_com(),
    m_66ip_cn(),
    vpnsocksfree_blogspot_com(),
    zhan_renren_com(),
    ip181_com(),
    nzhdnchk_blogspot_com(),
    hma-proxy_blogspot_com(),
    rsagartoolz_tk(),
    whhcrusher_wordpress_com(),
    ehacktricks_blogspot_com(),
    livesocksproxy_blogspot_com(),
    httpsdaili_com(),
    highanonymous_blogspot_com(),
    apexgeeky_blogspot_com(),
    hugeproxies_com(),
    sshfreemmo_blogspot_com(),
    kan339_cn(),
    googleproxydownload_blogspot_com(),
    phone-pro_blogspot_com(),
    dollar_bz(),
    dallasplace_biz(),
    hackers-workshop_net(),
    proxysplanet_blogspot_com(),
    buyproxy_ru(),
    gadaihpjakarta_com(),
    proxies24_wordpress_com(),
    pavel-volsheb_ucoz_ua(),
    vpntorrent_com(),
    demonproxy_blogspot_com(),
    tricksgallery_net(),
    turk-satelitforum_net(),
    nightx-proxy_blogspot_com(),
    czproxy_blogspot_com(),
    myhacktrick_heck_in(),
    frmmain_tk(),
    greatestpasswords_blogspot_com(),
    dailyhttpproxies_blogspot_com(),
    tarabe-internet_blogspot_com(),
    host-proxy7_blogspot_com(),
    vsocks_blogspot_com(),
    safersphere_co_uk(),
    proxiesus_blogspot_com(),
    atnteam_com(),
    daili666_net(),
    fazlateknik_blogspot_com(),
    freeproxiesx_blogspot_com(),
    quangninh-it_blogspot_com(),
    venezuela-proxy_blogspot_com(),
    proxybridge_com(),
    ay_25_html(),
    premiumproxies4u_blogspot_com(),
    adminym_com(),
    socksproxyip_blogspot_com(),
    ml(),
    ar51_eu(),
    gamerly_blogspot_com(),
    gurbetgulu_blogspot_com(),
    network54_com(),
    lexic_cf(),
    proxiescolombia_blogspot_com(),
    proxysock1_blogspot_com(),
    newfreshproxies24_blogspot_com(),
    edga_over-blog_com(),
    caratrikblogger_blogspot_com(),
    net_iphai_net(),
    freeproxylists_net(),
    angelfire_com(),
    sshfree247_blogspot_com(),
    freeproxy_org_ua(),
    everyhourproxy_blogspot_com(),
    darkgeo_se(),
    socks5listproxies_blogspot_com(),
    tiger-attack_forumotions_in(),
    ip004_com(),
    yourtools_blogspot_com(),
    internetproxies_wordpress_com(),
    sock_kteen_net(),
    llavesylicencia_blogspot_de(),
    proxy-list_net(),
    wumaster_blogspot_com(),
    blogprojesitr_blogspot_com(),
    tetraupload_net(),
    npmjs_com(),
    vizexmc_net(),
    unblocksites1_appspot_com(),
    proxy-base_org(),
    ip-pro-xy_blogspot_com(),
    realityforums_tk(),
    hdonnet_ml(),
    ezearn_info(),
    kendarilinux_org(),
    vseunas_mybb_ru(),
    freevipproxy_blogspot_com(),
    proxysockslist_blogspot_com(),
    incloak_es(),
    freewmz_at_ua(),
    ssor_net(),
    xfunc_ru(),
    soft_bz(),
    freeproxylistsdaily_blogspot_com(),
    variably_ru(),
    myforum_net_ua(),
    webextra_8m_com(),
    anzwers_org(),
    anyelse_com(),
    codediaries_com(),
    turkeycafe_net(),
    mixdrop_ru(),
    rumahhacking_tk(),
    cloudproxies_com(),
    persianwz_blogspot_com(),
    adminsarhos_blogspot_com(),
    proxy-good-list_blogspot_com(),
    mmotogether_blogspot_com(),
    freeproxydailydownload_blogspot_com(),
    elitecarders_name(),
    tehnofil_ru(),
    emailtry_com(),
    softwareflows_blogspot_com(),
    bestpollitra_com(),
    naijafinder_com(),
    proxy-worlds_blogspot_com(),
    scrapeboxforum_com(),
    freeproxy_seosite_in_ua(),
    tuoitreit_vn(),
    lukacyber_blogspot_com(),
    pawno_su(),
    proxies_org(),
    myproxy4me_blogspot_com(),
    paypalapi_com(),
    litevalency_com(),
    blackxpirate_blogspot_com(),
    boosterbotsforum_com(),
    torrenthane_net(),
    cybercobro_blogspot_com(),
    proxylistelite_blogspot_com(),
    ippipi_blogspot_com(),
    biskutliat_blogspot_com(),
    bestproxy_narod_ru(),
    searchgd_blogspot_com(),
    free-socks24_blogspot_com(),
    scrapeboxproxies_net(),
    fravia_2113_ch(),
    forums_techguy_org(),
    proxylust_com(),
    dailynewandfreshproxies_blogspot_com(),
    vip-skript_ru(),
    tolikon_chat_ru(),
    techsforum_vn(),
    caretofun_net(),
    freeproxygood_blogspot_com(),
    eemmuu_com(),
    mirclive_net(),
    turkbot_tk(),
    stopforumspam_com(),
    proxylist_sakura_ne_jp(),
    hostleech_blogspot_com(),
    megaseoforum_com(),
    seomicide_com(),
    hogwarts_bz(),
    proxiestoday_blogspot_com(),
    linhc_blog_163_com(),
    gbots_ga(),
    ir-linux-windows_mihanblog_com(),
    easyfreeproxy_com(),
    usaproxies_blogspot_com(),
    proxyharvest_com(),
    proxylistaz_blogspot_com(),
    proxword_blogspot_com(),
    mc6_info(),
    proxylists_me(),
    daily-proxies_webnode_fr(),
    s15_zetaboards_com(),
    proxyfreecopy_blogspot_com(),
    denza_pro(),
    vpncompare_co_uk(),
    eliteproxy_blogspot_com(),
    encikmrh_blogspot_com(),
    prx_biz(),
    sshdailyupdate_com(),
    ruproxy_blogspot_com(),
    ssh-proxies-socks_blogspot_com(),
    mymetalbusinesscardsservice_blogspot_com(),
    promicom_by(),
    cn-proxy_com(),
    kingdamzy_mywapblog_com(),
    myiptest_com(),
    rammstein_narod_ru(),
    google_com(),
    freessh-daily_blogspot_com(),
    proxyli_wordpress_com(),
    freeproxy-list_ru(),
    walksource_blogspot_com(),
    freeproxy_ru(),
    gridergi_8k_com(),
    proxylist-free_blogspot_com(),
    proxies247_com(),
    forumsblackmouse_blogspot_com(),
    blizzleaks_net(),
    bdaccess24_blogspot_com(),
    turkhackblog_blogspot_com(),
    mf158_com(),
    ghstools_fr(),
    proxyforest_com(),
    socks5_ga(),
    dailyfreehma_blogspot_com(),
    proxy-tr_blogspot_com(),
    free-proxy-list_appspot_com(),
    yanshi_icecms_cn(),
    forumtutkunuz_net(),
    proxies-zone_blogspot_com(),
    guncelproxy_com(),
    lee_at_ua(),
    dreamproxy_net(),
    sharevipaccounts_blogspot_com(),
    txt_proxyspy_net(),
    verifiedproxies_blogspot_com(),
    yahoo69fire_forumotion_net(),
    proxydz_blogspot_com(),
    promatbilisim_com(),
    freeblackhat_com(),
    d-hacking_blogspot_com(),
    getdailyfreshproxy_blogspot_com(),
    molesterlist_blogspot_com(),
    screenscrapingdata_com(),
    proxytime_ru(),
    black-socks24_blogspot_com(),
    goldenproxies_blogspot_com(),
    fasteliteproxyserver_blogspot_com(),
    rmccurdy_com(),
    myunlimitedways_blogspot_com(),
    vkcompsixo9_blogspot_com(),
    baizhongsou_com(),
    proxylistmaster_blogspot_com(),
    proxylist_net(),
    awmproxy_cn(),
    boxgods_com_websitedetective_net(),
    subiectiv_com(),
    arpun_com(),
    changeips_com(),
    ahmadqoisja09_blogspot_com(),
    shannox1337_wordpress_com(),
    multi-cheats_com(),
    dailyfreshproxies4u_wordpress_com(),
    munirusurajo_blogspot_com(),
    tricksmore1_blogspot_com(),
    proxy-z0ne_blogspot_com(),
    hackingproblemsolution_wordpress_com(),
    proxy_com_ru(),
    proxiesz_blogspot_com(),
    proxy_3dn_ru(),
    dlfreshfreeproxy_blogspot_com(),
    iphai_com(),
    freshliveproxies_blogspot_com(),
    proxanond_blogspot_com(),
    anonfreeproxy_blogspot_com(),
    globalproxies_blogspot_com(),
    proxylivedaily_blogspot_com(),
    cyber-gateway_net(),
    android-amiral_blogspot_com(),
    ufcommunity_com(),
    socks24_crazy4us_com(),
    jekkeyblog_ru(),
    checkyounow_com(),
    ip_downappz_com(),
    steam24_org(),
    proxytm_com(),
    getfreestuff_narod_ru(),
    updatedproxylist_blogspot_com(),
    fenomenodanet_blogspot_com(),
    hamzatricks_blogspot_com(),
    ubuntuforums_org(),
    yunusortak_blogspot_com(),
    proxy14_blogspot_com(),
    irc-proxies_blogspot_com(),
    makeuseoftechmag_blogspot_com(),
    proxy-hell_blogspot_com(),
    vietnamworm_org(),
    gratisproxylist_blogspot_com(),
    uub7_com(),
    anon-proxy_ru(),
    proxyfine_com(),
    http-proxy_ru(),
    made-make_ru(),
    proxies4google_com(),
    domainnameresellerindia_com(),
    devil-group_com(),
    rranking6_ziyu_net(),
    freeproxiesforall_blogspot_com(),
    proxy3e_blogspot_com(),
    haodailiip_com(),
    workingproxies_org(),
    multiproxy_org(),
    us-proxyservers_blogspot_com(),
    proxysockus_blogspot_com(),
    veryownvpn_com(),
    emillionforum_com(),
    robotproxy_com(),
    fastanonymousproxies_blogspot_com(),
    blog_mobilsohbetodalari_org(),
    cdn_vietso1_com(),
    bagnets_cn(),
    proxyforyou_blogspot_com(),
    freessl-proxieslist_blogspot_com(),
    chingachgook_net(),
    artgameshop_ru(),
    proxium_ru(),
    freeproxyserverus_blogspot_com(),
    us-proxy_org(),
    proxymore_com(),
    mixadvigun_blogspot_com(),
    aw-reliz_ru(),
    exilenet_org(),
    glowinternet_blogspot_com(),
    technaij_com(),
    howaboutthisproxy_blogspot_com(),
    russianproxies24_blogspot_com(),
    bestpremiumproxylist_blogspot_com(),
    proxyrox_com(),
    hackage_haskell_org(),
    proxyrss_com(),
    coobobo_com(),
    andrymc4_blogspot_com(),
    forum_6cn_org(),
    forum_icqmag_ru(),
    thefreewebproxies_blogspot_com(),
    memberarea_my_id(),
    en_proxy_net_pl(),
    privatedonut_com(),
    madproxy_blogspot_com(),
    proxylistworking_blogspot_com(),
    amisauvveryspecial_blogspot_com(),
    socksproxylist24_blogspot_com(),
    checkedproxylists_com(),
    chinaproxylist_wordpress_com(),
    forums_openvpn_net(),
    lb-ru_ru(),
    latestcrackedsoft_blogspot_com(),
    bsproxy_blogspot_com(),
    daily-freeproxylist_blogspot_com(),
    dodgetech_weebly_com(),
    good-proxy_ru(),
    anonimseti_blogspot_com(),
    livesocks_net(),
    freetip_cf(),
    daily-freshproxies_blogspot_com(),
    exlpoproxy_blogspot_com(),
    peterarmenti_com(),
    blackhatseo_pl(),
    haozs_net(),
    wikimapia_org(),
    freetao_org(),
    freshsocks5_blogspot_com(),
    ushttpproxies_blogspot_com(),
    update-proxyfree_blogspot_com(),
    mircindirin_blogspot_com(),
    codeddrago_wordpress_com(),
    automatedmarketing_wplix_com(),
    proxyfire_wordpress_com(),
    donkyproxylist_blogspot_com(),
    punjabicheetay_blogspot_com(),
    proxyso_blogspot_com(),
    susanin_nm_ru(),
    foxtools_ru(),
    forumustasi_com(),
    warriorforum_com(),
    w3bies_blogspot_com(),
    unblockinternet_blogspot_com(),
    uiauytuwa_blogspot_com(),
    europeproxies_blogspot_com(),
    blackhatprivate_com(),
    freenew_cn(),
    beautydream_spb_ru(),
    alivevpn_blogspot_com(),
    botgario_ml(),
    shram_kiev_ua(),
    proxytut_ru(),
    download_menghitung_id(),
    proxy_pressleib_info(),
    goodenproxy_blogspot_com(),
    blackhatprotools_org(),
    bulkinfo_net(),
    ever-click_com(),
    jumperpot_blogspot_com(),
    qtduokaiqi_com(),
    topgoldforum_com(),
    lawinaboard_com(),
    usafastproxy_blogspot_com(),
    anonproxylist_blogspot_com(),
    ecindustry_ru(),
    blackhatviet_com(),
    free_proxy-sale_com(),
    proxyip_blogspot_com(),
    socks4proxy50_blogspot_com(),
    proxy-list_org(),
    linkstown_de(),
    daddytragedy_net(),
    blog_crackitindonesia_com(),
    vipsocks24_net(),
    vipsock24_blogspot_com(),
    idcloak_com(),
    ipsocks_blogspot_com(),
    rpn_gov_ru(),
    fastmoney_cc(),
    proxiesfree_blogspot_com(),
    robtex_com(),
    proxyitblog_blogspot_com(),
    htmlweb_ru(),
    socks5proxywordpress_blogspot_com(),
    ptc-game_info(),
    skyul_com(),
    data-serv_org(),
    tp2k1_blogspot_com(),
    umbrella-security_blogspot_com(),
    facebookcybertricks_blogspot_com(),
    sentrymba-turkey_blogspot_com(),
    proxyref_ru(),
    ordan-burdan_blogcu_com(),
    cz88_net(),
    lab_magicvox_net(),
    img_sl-ok_com(),
    freesocks5proxybest_blogspot_com(),
    extractionscraping_com(),
    nwop_org(),
    proxyday_blogspot_com(),
    trproxy_blogspot_com(),
    naijasite_com(),
    proxieslounge_blogspot_com(),
    hmaproxies_blogspot_com(),
    jimicaters_tk(),
    hackthrone_com(),
    contournerhadopi_com(),
    tt_net&show_results=posts(),
    proxy-fresh_ru(),
    arianatorleaks_com(),
    freeproxylists_co(),
    windowsfastipchanger_com(),
    coolproxies_blogspot_com(),
    useliteproxy_blogspot_com(),
    ultimatecheckedproxy_blogspot_com(),
    autoproxyblog_wordpress_com(),
    socksproxyblog_blogspot_com(),
    pgate_kr(),
    ip_proxyfire_net(),
    freshdailyfreeproxy_blogspot_com(),
    frmmavi_com(),
    proxylist_proxypy_org(),
    gencfrm_net(),
    craftcadia_com(),
    noname_zone(),
    proxylistfree24h_blogspot_com(),
    blog_sina_com_cn(),
    xxxsuzdalxxx_ru(),
    alexa_lr2b_com(),
    freevpn_ninja(),
    newdaily-proxy_blogspot_com(),
    madleets_com(),
    techtym_info(),
    asua_esy_es(),
    x-admin_ru(),
    tophacksavailable_blogspot_com(),
    proxyworld_us(),
    siberforum_org(),
    icq-num_ru(),
    seo_hot_az_pl(),
    aircrack_kl_com_ua(),
    freeproxylisteveryday_blogspot_com(),
    matio13com_blogspot_com(),
    socksdownload_blogspot_com(),
    toolandtool_blogspot_com(),
    emucoach_com(),
    socks66_blogspot_com(),
    awmproxy_com(),
    qpae_activeboard_com(),
    freeproxy80_blogspot_com(),
    fastproxy1_com(),
    proxylistdailys_blogspot_com(),
    proxy-server-usa_blogspot_com(),
    best-proxy-list-ips_blogspot_com(),
    forumleaders_com(),
    new-daily-proxies_blogspot_com(),
    netzoom_ru(),
    getfreeproxies_blogspot_com(),
    newsproxy_blogspot_com(),
    freefastproxys_com(),
    la2-bag_at_ua(),
    proxytodays_blogspot_com(),
    eg-proxy_blogspot_com(),
    clientn_autohideip_com(),
    upd4tproxy_blogspot_com(),
    marcosbl_com(),
    proxysample_blogspot_com(),
    ip_zdaye_com(),
    mail_nyetok_com(),
    zahodi-ka_ru(),
    free-proxy_3dn_ru(),
    proxyzheaven_blogspot_com(),
    temcom_h1_ru(),
    chatles_net(),
    sharecodetravinh_blogspot_com(),
    proxy_auditoriaswireless_org(),
    warezinturkey_wordpress_com(),
    socks5update_blogspot_com(),
    glproxy_blogspot_com(),
    netzwelt_de(),
    myfreeproxy4all_blogspot_com(),
    safe-proxy_club(),
    royaltvpro_com(),
    superfastproxy_blogspot_com(),
    thebotnet_com(),
    taofuli8_com(),
    quick-hide-ip_com(),
    proxy_atomintersoft_com(),
    chinaproxies_blogspot_com(),
    guncelproxy-list_blogspot_com(),
    proxies_iamthedave_com(),
    testsacdienthoai_com(),
    httpsproxyfree_blogspot_com(),
    hizliproxyler_blogspot_com(),
    vseskachat_net(),
    forumrenkli_com(),
    lofter_com(),
    vietnamproxies_blogspot_com(),
    socks5xrumer_blogspot_com(),
    proxday_blogspot_com(),
    nishadraj_blogspot_com(),
    proxy_chnlanker_com(),
    hide-me_ru(),
    hiliart_wordpress_com(),
    proxy-dailyupdate_blogspot_com(),
    rhproxy_blogspot_com(),
    kxdaili_com(),
    ahmadas120873_blogspot_com(),
    mircarsivi_wordpress_com(),
    vipsocks5live_blogspot_com(),
    pasokoma_jp(),
    ipodtouchtocomputer_com(),
    liuliangwang_net(),
    storecvv_net(),
    freesocks5proxies_com(),
    freshtechnologys_blogspot_com(),
    tools_rosinstrument_com(),
    proxy-crazy_blogspot_com(),
    hackxcrack_es(),
    anonymousproxies007_blogspot_com(),
    craiglistproxies_blogspot_com(),
    naimirc_blogspot_com(),
    ast-post(),
    rdpcheap_com(),
    awmproxy_de(),
    hpc_name(),
    vip-socks24_blogspot_com(),
    qiangkezu_com(),
    vpnhook_com(),
    bestblackhattips_blogspot_com(),
    freshnewproxies1_blogspot_com(),
    proxygratuit1_rssing_com(),
    web-data-scraping_com(),
    china-eliteproxy_blogspot_com(),
    rationonestpax_net(),
    hohoproxy_blogspot_com(),
    farfree_cn(),
    mailsys_us(),
    freeproxyserevr_blogspot_com(),
    spaintersquad_xyz(),
    webanetlabs_net(),
    google-proxy_net(),
    krachas_itgo_com(),
    darknetforums_com(),
    topgen_net(),
    poproxy_blogspot_com(),
    theultimatebuzzfeed_blogspot_com(),
    proxy_wow_ag(),
    thizmornin_blogspot_com(),
    sehirlersohbet_com(),
    ************_com(),
    completes_ru(),
    tapakiklan_com(),
    proxyservers_pro(),
    forumsevgisi_com(),
    huong-dan-kiem-tien-qua-mang_blogspot_com(),
    tricksvpro_com(),
    ilegalhunting_site(),
    elite-proxies_blogspot_com(),
    freshsocks45_blogspot_com(),
    bbs_crsky_com(),
    m3hr1_blogspot_com_tr(),
    proxy-daily_com(),
    blog_qualityproxylist_com(),
    dailyfreeproxy-list_blogspot_com(),
    webanet_ucoz_ru(),
    kuron-zero_com(),
    vipsockslive_blogspot_com(),
    share-socks_blogspot_com(),
    proxyorg_blogspot_com(),
    www-proxies_com(),
    opnewblood_com(),
    proxy-server-free-list_blogspot_com(),
    darkbyteblog_wordpress_com(),
    ibrahimfirat_net(),
    api_foxtools_ru(),
    truehackers_ru(),
    absentius_narod_ru(),
    socksproxies24_blogspot_com(),
    forumask_net(),
    soproxynew_blogspot_com(),
    cheap-offers-online_com(),
    asdfg3ffasdasdasd_blogspot_com(),
    boomplace_com(),
    primiumproxy_blogspot_com(),
    m_hackops_tr_gg(),
    igosu_ru(),
    the-plick_blogspot_com(),
    gentospasport-usbmultiboot_blogspot_com(),
    vietincome_vn(),
    ref5_net(),
    ip_socksproxychecker_com(),
    socks-list_blogspot_com(),
    feiyan9_com(),
    ecuadorproxy_blogspot_com(),
    proxies_blamend_com(),
    vipsocks5-frwjjwesh_blogspot_com(),
    guncelproxy_net(),
    boltonnewyork_blogspot_com(),
    ssh-indo_blogspot_com(),
    socks-agario_blogspot_com(),
    reportintrend_com(),
    parsemx_com(),
    topproxys_blogspot_com(),
    free-proxy-list-naim_blogspot_com(),
    newdaily-proxies_blogspot_com(),
    anonymousproxyblog_blogspot_com(),
    sockdaily_org(),
    goldensocksdownload_blogspot_com(),
    proxy-nod32_blogspot_com(),
    mischakot_jimdo_com(),
    tipstricksinternet_blogspot_com(),
    portsaid-tazalomat_ddns_net(),
    mytricksanddownloads_blogspot_com(),
    vipsocks32_blogspot_com(),
    freedailyproxy_com(),
    proxieportal_blogspot_com(),
    indonesiancarding_net(),
    u-hackiing_blogspot_com(),
    raying_ru(),
    facebook_com(),
    xaker_name(),
    inav_chat_ru(),
    fineproxy_de(),
    genuineproxy_blogspot_com(),
    twitgame_ru(),
    onlyfreeproxy_blogspot_com(),
    proxy-updates_blogspot_com(),
    getdailyproxy_blogspot_com(),
    trsohbetsiteleri_gen_tr(),
    proxymasterz_wordpress_com(),
    blog_i_ua(),
    paksoftweres_blogspot_de(),
    eliteproxiesdailyfree_blogspot_com(),
    fldd_cn(),
    lan26766_cn(),
    coooltricks4u_blogspot_com(),
    lxd-proxy-list_blogspot_com(),
    susqunhosting_net(),
    skidsbb_net(),
    awmproxy_com_ua(),
    linongon_com(),
    dichvusocks_wordpress_com(),
    elitesocksproxy_blogspot_com(),
    zoproxy_blogspot_com(),
    proxieblog_blogspot_com(),
    mellyvsstomach_blogspot_com(),
    sbproxies_blogspot_com(),
    czechproxy_blogspot_com(),
    proxyshack_com(),
    i-socks_blogspot_com(),
    freeipproxy_space(),
    usaproxynetwork_blogspot_de(),
    android-ful_blogspot_com(),
    kualatbabi2_blogspot_com(),
    kingproxies_com(),
    vip-socks25_blogspot_com(),
    uuip_net(),
    ajshw_net(),
    uptodateproxies_blogspot_com(),
    seogadget_panelstone_ru(),
    firdouse_heck_in(),
    freepublicproxies_blogspot_com(),
    kaosyazar_blogspot_com(),
    googleproxydaily_blogspot_com(),
    serverproxyfreelist_blogspot_com(),
    freshlyproxy_blogspot_com(),
    cardersforum_ws(),
    brothers-download_mihanblog_com(),
    m_ultrasurf_org(),
    proxefr_blogspot_de(),
    showproxy_blogspot_com(),
    proxy-masterz_blogspot_com(),
    system32virus_blogspot_com(),
    uydu-destek_com(),
    tenthandfive_collected_info(),
    indexdl_com(),
    ad_qatarad_com(),
    yuttjythgrersrtyrtdt_blogspot_com(),
    crackingheaven_com(),
    flvmplayerv3_blogspot_com(),
    freecenter_h1_ru(),
    proxyibet_blogspot_com(),
    proxies_by(),
    proxylistfree_ml(),
    sslproxies101_blogspot_com(),
    fatpeople_lol(),
    web_freerk_com(),
    mmm-downloads_at_ua(),
    vectordump_com(),
    socks5_net_ipaddress_com(),
    premiumad_blogspot_com(),
    blogspotproxy_blogspot_com(),
    ebubox_com(),
    customproxy_blogspot_com(),
    blackhatseocommunity_com(),
    exchangecurrencyzone_com(),
    proxytrue_ml(),
    darkbull_net(),
    yuxingyun_com(),
    unlimitedproxylist_blogspot_com(),
    dlnxt_com(),
    kraken-net_ru(),
    files_c75_in(),
    bestblackhatforum_com(),
    proxyhosting_ru(),
    bitorder_ru(),
    maxiproxies_com(),
    nededik_com(),
    ifstar_net(),
    tml(),
    proxy-poster_blogspot_com(),
    download-windows-linux_us(),
    specquote_com(),
    geekelectronics_org(),
    www2_waselproxy_com(),
    white55_narod_ru(),
    ww_57883_com(),
    socks5vips_blogspot_com(),
    guncelproxys_blogspot_com(),
    forumobil_com(),
    waselproxy_com(),
    comp0_ru(),
    socks5-lists_blogspot_com(),
    proxydb_net(),
    buytrafficus_blogspot_com(),
    langamepp_com(),
    intelegenci_blogspot_de(),
    proxynew_ml(),
    proxytwit_blogspot_com(),
    hostssh_blogspot_com(),
    blog_kuaidaili_com(),
    liveproxysocks_blogspot_com(),
    lovefreexy_com(),
    newproxylist24_blogspot_com(),
    tricksaddict_blogspot_com(),
    proxiesplanet_blogspot_com(),
    hackerzcity_com(),
    shujuba_net(),
    blog_baotuoitredoisong_com(),
    blue-hosting_biz(),
    freshfreeproxylist_wordpress_com(),
    admintuan_com(),
    hakerstrong_ru(),
    socks5-proxies_blogspot_com(),
    proxy_rufey_ru(),
    surfanonymously_blogspot_com(),
    sock5us_blogspot_com(),
    duslerforum_org(),
    meilleurvpn_org(),
    ejohn_org(),
    eliteproxygiveaway_blogspot_com(),
    eliteproxieslist_blogspot_com(),
    forumsevdasi_com(),
    fpteam-hack_com(),
    torstatus_blutmagie_de(),
    rahultrick_blogspot_com(),
    api_good-proxies_ru(),
    proxy-ip_net(),
    proxyprivat_com(),
    mitituti_com(),
    sohbetetsem_com(),
    cyberleech_in(),
    vipsocks5-fresh_blogspot_com(),
    new-socks5_cf(),
    forumplus_biz(),
    scripts_izviet_net(),
    gencturkfrm_com(),
    crackwarrior_org(),
    packetpunks_net(),
    rustyhacks_blogspot_com(),
    freeproxylist69_blogspot_com(),
    members_lycos_co_uk(),
    update-proxyfree1_blogspot_com(),
    colombiaproxies_blogspot_com(),
    freeproxyserverliste_blogspot_com(),
    forumzirve_net(),
    fresh-proxies-list_blogspot_com(),
    niceproxy_blogspot_com(),
    prime-proxies_blogspot_com(),
    premium-guncel_blogspot_com(),
    mysafesurfing_com(),
    vtlingyu_lofter_com(),
    vectroproxy_com(),
    mollaborjan_com(),
    socks_kunvn_com(),
    freeproxyblog_wordpress_com(),
    forums_mukamo_org(),
    abu-alayyam_8m_com(),
    freevpnaccounts_wordpress_com(),
    bestproxiesandsocks_blogspot_com(),
    anonproxylist_com(),
    vpteam_net(),
    fakemyip_info(),
    gettheproxy_blogspot_com(),
    httpdaili_com(),
    updatenewproxylist_blogspot_com(),
    geeknotweak_blogspot_com(),
    proxy-kurdsoft_blogspot_com(),
    proxy_loresoft_de(),
    wu-carding_com(),
    chupadrak_i8_com(),
    xxooxxbb_blog_163_com(),
    todayfreeproxylist_blogspot_com(),
    list-socks5_blogspot_com(),
    checkproxylist_blogspot_com(),
    elitehackforums_net(),
    proxy-masterz_com(),
    proxyjunction_blogspot_com(),
    proxyocean_com(),
    fastproxylist_blogspot_com(),
    dailitk_tk(),
    webupon_com(),
    proxy-pack_blogspot_com(),
    proxytxt_blogspot_com(),
    qq_mcbang_com(),
    cpahero_xyz(),
    hayatforumda_com(),
    cyberleech_com(),
    hoztovary_tomsk_ru(),
    sysnet_ucsd_edu(),
    webacard_com(),
    undermattan_com(),
    trkua_com(),
    socks51080proxy_blogspot_com(),
    svkhampha_blogspot_com(),
    bali-xp_com(),
    cyberleech_info(),
    uscarschannel_blogspot_com(),
    en_soks_biz(),
    vmarte_com(),
    leechproxy_blogspot_com(),
    cqcounter_com(),
    yogapasa_blogspot_com(),
    premiumaccountmalaysia_blogspot_com(),
    win7sky_com(),
    freshproxylistupdate_blogspot_com(),
    gatherproxy80_blogspot_com(),
    backtrack-gr0up_blogspot_com(),
    dev-spam_com(),
    chenqu_wordpress_com(),
    files_1337upload_net(),
    rayhack_ru(),
    surveybypass4u_blogspot_com(),
    johnstudio0_tripod_com(),
    freeproxyandvpn_blogspot_com(),
    reptv_ru(),
    sockproxies_blogspot_com(),
    detrasdelaweb_com(),
    sites_google_com(),
    rasyidmaulanafajar_blogspot_com(),
    socksproxyfree_blogspot_com(),
    greekhacking_gr(),
    huaxiaochao_com(),
    freedailyproxies_wordpress_com(),
    freepublicproxyserverlist_blogspot_com(),
    proxymaxs_blogspot_com(),
    hackforums_ru(),
    guncelproxyler_blogspot_com(),
    proxyfreedaily_com(),
    roxproxy_com(),
    x-cyb3r_blogspot_com(),
    socks5sonline_blogspot_com(),
    cardersforum_se(),
    proxysite-list_blogspot_com(),
    pr0xy-land_blogspot_com(),
    proxies_pressleib_info(),
    ip3366_net(),
    absence_sharewarecentral_com(),
    g44fun_blogspot_com(),
    vip-socks-online_blogspot_com(),
    morph_io(),
    blog_gmane_org(),
    resourceforums_net(),
    ip_baizhongsou_com(),
    socks5listblog_blogspot_com(),
    awmproxy_net(),
    turkishmodders_blogspot_com(),
    eliteproxy-list_blogspot_com(),
    mc_sparkgames_ru(),
    gamasaktya_wordpress_com(),
    rogerz404_blogspot_com(),
    vip-socks5server_blogspot_com(),
    mypremiumproxieslists_blogspot_com(),
    cooleasy_com(),
    dev_seogift_ru(),
    fineproxy_org(),
    fresshproxys_blogspot_com(),
    proxy-mmo_blogspot_com(),
    proxy_iamthedave_com(),
    forum_worldofhacker_com(),
    freeusproxyserverlist_blogspot_com(),
    proxiesatwork_com(),
    socks5proxyus_blogspot_com(),
    epstur_blogspot_com(),
    proxy-list_biz(),
    onlinewazir_com(),
    freelive-socks_blogspot_com(),
    flipperweb9947_wordpress_com(),
    wolfhyper_com(),
    blog_donews_com(),
    proxiesblog_wordpress_com(),
    restproxy_com(),
    rhega_net(),
    proxyblog_co(),
    keyloggerfacebookhack4c_wordpress_com(),
    socks124_blogspot_com(),
    free-proxy-sock5_leadervpn_com(),
    turklive_tk(),
    proxies1080_blogspot_com(),
    shak779_blogspot_com(),
    pulsitemeter_com(),
    proxyhexa_blogspot_com(),
    indonesiaproxies_blogspot_com(),
    newhttpproxies_blogspot_com(),
    proxyzeswiata_blogspot_com(),
    crackpctools_blogspot_com(),
    indysosoman_blogspot_de(),
    pchelka_trade(),
    freeschoolproxy_blogspot_com(),
    bestproxylist4you_blogspot_com(),
    imtalk_org(),
    proxys_com_ar(),
    proxyblogspot_blogspot_com(),
    pengguna-gadget_blogspot_de(),
    proxyips_com(),
    proxy-now_blogspot_com(),
    iptrace_me(),
    vpn_hn-seo_com(),
    cybersocks404_blogspot_com(),
    digitalwarfare_ru(),
    bestproxiesofworld_blogspot_com(),
    hack-crack_fr(),
    hidemyass-proxy-daily_blogspot_com(),
    page2rss_com(),
    vietkhanh_me(),
    proxylist_me(),
    scrapeproxy_blogspot_com(),
    xicidaili_com(),
    kikil_info(),
    myelliteproxylist_blogspot_com(),
    freeproxydailyupdate_blogspot_com(),
    actualproxy_blogspot_com(),
    maviware_com(),
    ebookinga_com(),
    phcorner_net(),
    proxiak_pl(),
    crackingless_com(),
    forumoloji_com(),
    full-proxies_blogspot_de(),
    proxy_darkbyte_ru(),
    proxysoku_blogspot_com(),
    indoproxy_blogspot_com(),
    ucundaa_com(),
    lifehaker_forum-top_ru(),
    proxyparadise_blogspot_com(),
    blackhatteam_com(),
    proxysky4you_blogspot_com(),
    goldensocksproxyworld_blogspot_com(),
    t2624_blogspot_com(),
    foroblackhat_com(),
    link-im_blogspot_com(),
    proxyandproxytools_blogspot_com(),
    bugs4u_info(),
    sergei-m_narod_ru(),
    p2pchedai_info(),
    proxyfor_eu(),
    triksshterbaru_blogspot_com(),
    ssldailyproxies_blogspot_com(),
    masudakmmc5_blogspot_com(),
    proxy-serversites_blogspot_com(),
    square_blog_cnstock_com(),
    proxyaz_blogspot_com(),
    lxd-proxy-lists_blogspot_com(),
    rainng_com(),
    proxies_cz_cc(),
    siteownersforums_com(),
    test-backs_blogspot_de(),
    free-anonymous-proxy-lists-daily_blogspot_com(),
    filka_cc(),
    sslproxylist_net(),
    digitalcybersoft_com(),
    giatmaraweekly_blogspot_com(),
    mfqqx_com(),
    hilevadisi_com(),
    lamoscar-nnt_com(),
    freshdailyproxyblog_blogspot_com(),
    socksnproxy_blogspot_com(),
    dronmovie_blogspot_com(),
    proxy-lists_net(),
    newsslproxies_blogspot_com(),
    cloudfsx_com(),
    sockfree_net(),
    disk_make_im(),
    goodproxies_blogspot_com(),
    tcybers_net(),
    baizhongso_com(),
    getfreedailyproxy_blogspot_com(),
    likerworld_com(),
    proxyfile_ru(),
    szwindoor_com(),
    forum_bih_net_ba(),
    foruminfo_net(),
    freshproxyservers_blogspot_com(),
    white-socks24_blogspot_com(),
    seogadget_ru(),
    th88520_lofter_com(),
    sockvips5_org(),
    freehidemyasspremiumproxy_blogspot_com(),
    leogaproksi_blogspot_com(),
    hide-my-ip_com(),
    megamoneyideas_com(),
    vipsocks_in(),
    eamao_com(),
    goldproxy1_blogspot_com(),
    fullcode_com_vn(),
    carder_site(),
    best-proxylist_blogspot_com(),
    softversions_blogspot_de(),
    dailyfreeproxies_blogspot_com(),
    fresh-proxy-list_org(),
    hackerz5_com(),
    hedefsohbet_net(),
    free-proxy-server-list-ip_blogspot_com(),
    orca_tech(),
    moneyfanclub_com(),
    fa_blog_asl19_org(),
    askimiz_net(),
    frmkeyfi_blogspot_com(),
    simpleproxy_ru(),
    cannabis-samen-hanfsamen_de(),
    agarioproxys_blogspot_com(),
    sea55_com(),
    myfreeproxies_com(),
    programinifullindir_blogspot_de(),
    moreproxies_blogspot_com(),
    pro_tristat_org(),
    socksproxy4all_blogspot_com(),
    proxy-heaven_blogspot_it(),
    totalblackhat_com(),
    dynamic-marketing_blogspot_com(),
    prologic_su(),
    bbhfdownloads_blogspot_com(),
    scrapeboxfreeproxy_blogspot_com(),
    nigger_re(),
    proxy-heaven_blogspot_ro(),
    techaroid_com(),
    freepasstransport_blogspot_com(),
    petrmoney1_narod_ru(),
    netyazari_com(),
    agat_net_ru(),
    anonymouscyberteamact_blogspot_com(),
    clientn_real-hide-ip_com(),
    blackhatland_com(),
    crackingfire_net(),
    xakepok_ucoz_net(),
    proxyserverlistaz_blogspot_com(),
    liveproxyeveryday_blogspot_com(),
    daily-proxylists_blogspot_com(),
    kamlut_blogspot_com(),
    xy_html(),
    proxy-socks4a_blogspot_com(),
    forum_eve-ru_com(),
    superproxylist_blogspot_com(),
    girlsproxy_blogspot_com(),
    xici_net_co(),
    utenti_lycos_it(),
    proxyserver-list_blogspot_com(),
    forum_uinsell_net(),
    sonisontel_blogspot_com(),
    kevinvarghese_blogspot_com(),
    broadbandreports_com(),
    ipportable_blogspot_com(),
    knight2k1_blogspot_com(),
    kodevreni_com(),
    nixyia_net(),
    http-proxy-list_ru(),
    aimeegracecatering_blogspot_com(),
    okvpn_org(),
    premium-downloadz_blogspot_com(),
    hack72_2ch_net(),
    foro20_com(),
    norescheat_blogspot_com(),
    proxy_muvc_net(),
    socks14_blogspot_com(),
    akatsukihackblog_blogspot_com(),
    tricksass_blogspot_com(),
    vietso1_com(),
    white55_ru(),
    proxyarsivi_com(),
    updated-proxies_blogspot_com(),
    privateproxysocks_blogspot_com(),
    dailyproxylistfree_blogspot_com(),
    webmaster-alexander_blogspot_com(),
    nmsec_wordpress_com(),
    freefakeip_blogspot_com(),
    youdaili_net(),
    team-world_net(),
    vn-zoom_com(),
    hideme_ru(),
    masalca_net(),
    proxylist101_blogspot_com(),
    mpgh_net(),
    xijie_wordpress_com(),
    adan82222_blogspot_com(),
    atcpu_com(),
    guncelproksi_blogspot_com(),
    antynet_pl(),
    indri-n-friends_super-forum_net(),
    hack-z0ne_blogspot_com(),
    nulledbb_com(),
    shjsjhsajasjssaajsjs_esy_es(),
    transparentproxies_blogspot_com(),
    myfreehmaproxies_blogspot_com(),
    anonymous-proxy_blogspot_com(),
    forum-haxs_ru(),
    free-ssl-proxyy_blogspot_com(),
    hacking-articles_blogspot_de(),
    indoblog_me(),
    falcontron_blogspot_com(),
    codezr_com(),
    wlshw_com(),
    qxmuye_com(),
]
