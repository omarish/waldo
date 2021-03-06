#!/usr/bin/env python

import os
import random
import tornado.options
import tornado.httpclient
from tornado.options import define, options, parse_command_line
from tornado.httpserver import HTTPServer
import tornado.gen
import tornadoredis
import logging
logger = logging.getLogger(__name__)
import heapq

from config import redis_config, redis_channel
from finders.flatfile import Flatfile
from finders.proxyspy import ProxySpy

tornado.httpclient.AsyncHTTPClient.configure(
    "tornado.curl_httpclient.CurlAsyncHTTPClient")

define("debug", default=True, type=bool, help="debug mode?")
define("port", default=1234, type=int, help="expose waldo on which port?")
define("loglevel", default='INFO', type=str, help='logging level')

pth = lambda x: os.path.join(os.path.dirname(__file__), x)

redis_conn = tornadoredis.Client(**redis_config)
redis_conn.connect()


class ProxyServer(HTTPServer):
    http_client = tornado.httpclient.AsyncHTTPClient()
    fatal_error_codes = (403, 599)

    def __init__(self, *args, **kwargs):
        self.user_agents = open(pth('user_agents.txt')).readlines()
        self.debug = kwargs.pop("debug", False)
        proxies = self.load_proxies()
        heapq.heapify(proxies)
        self.proxies = proxies
        super(ProxyServer, self).__init__(self.handle_request, **kwargs)

    def load_proxies(self, finders=[ProxySpy]):
        proxies = set()
        for finder in finders:
            new_proxies = finder().get_all()
            proxies.update(new_proxies)
            logging.info("%s discovered %s proxies." % (finder.__name__,
                                                        len(new_proxies)))
        logging.info("Added %s new proxies." % len(proxies))
        return list(proxies)

    def get_proxy(self):
        return heapq.heappop(self.proxies)

    def restore_proxy(self, proxy):
        heapq.heappush(self.proxies, proxy)

    @tornado.gen.engine
    def handle_request(self, request):
        if not 'User-Agent' in request.headers:
            request.headers['User-Agent'] = random.choice(self.user_agents)

        success, tries = False, 10
        while (not success and tries > 0):
            try:
                proxy = self.get_proxy()
            except IndexError:
                # Not enough proxy servers.
                request.write("HTTP/1.1 429\r\n"\
                    "Waldo is receiving too many inbound requests."\
                    "Try again soon.")
                request.finish()

            try:
                response = yield self.http_client.fetch(request.uri,
                                                        headers=request.headers,
                                                        request_timeout=5,
                                                        **proxy.connection_attrs
                                                        )
            except Exception, e:
                logging.error(e)
                if hasattr(e, 'code'):
                    status_code = e.code
                    redis_conn.publish(redis_channel, status_code)
                    if not status_code in fatal_error_codes:
                        # If it's a non-terminal error, add the proxy back to the queue.
                        self.restore_proxy(proxy)
                else:
                    status_code = None
                tries -= 1
            else:
                redis_conn.publish(redis_channel, response.code)
                logging.info("Success")
                success = True
                self.restore_proxy(proxy)
                request.write("HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n%s" %
                          (len(response.body), response.body))
        if tries == 0 and not success:
            # The request failed because of too many retries.
            request.write("HTTP/1.1 503")
        request.finish()


if __name__ == '__main__':
    io_loop = tornado.ioloop.IOLoop.instance()
    parse_command_line()
    http_server = ProxyServer(io_loop=io_loop, debug=options.debug)
    http_server.listen(options.port)
    logging.basicConfig(level=getattr(logging, options.loglevel))
    io_loop.start()
