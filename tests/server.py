#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import base64
import functools
from inspect import isawaitable
from pathlib import Path
from typing import Any, Callable, Union, List, Type, Awaitable, Dict
from urllib.parse import urlparse

from tornado import web
from tornado.httputil import HTTPServerRequest
from tornado.log import access_log
from tornado.routing import _RuleList

from pyppeteer.util import get_free_port

BASE_HTML = '''
<html>
<head><title>main</title></head>
<body>
<h1 id="hello">Hello</h1>
<a id="link1" href="./1">link1</a>
<a id="link2" href="./2">link2</a>
</body>
</html>
'''


class BaseHandler(web.RequestHandler):
    def get(self) -> None:
        self.set_header(
            'Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0',
        )


class MainHandler(BaseHandler):
    def get(self) -> None:
        super().get()
        self.write(BASE_HTML)


class EmptyHandler(BaseHandler):
    def get(self) -> None:
        super().get()
        self.set_status(204)
        self.write('')


class LongHandler(BaseHandler):
    async def get(self) -> None:
        super().get()
        await asyncio.sleep(0.5)
        self.write('')


class LinkHandler1(BaseHandler):
    def get(self) -> None:
        super().get()
        self.set_status(200)
        self.write(
            '''
<head><title>link1</title></head>
<h1 id="link1">Link1</h1>
<a id="back1" href="./">back1</a>
        '''
        )


class RedirectHandler1(BaseHandler):
    def get(self) -> None:
        super().get()
        self.redirect('/redirect2')


class RedirectHandler2(BaseHandler):
    def get(self) -> None:
        super().get()
        self.write('<h1 id="red2">redirect2</h1>')


class RedirectHandler3(BaseHandler):
    def get(self) -> None:
        super().get()
        self.redirect('/assets/one-frame.html')


class ResourceRedirectHandler(BaseHandler):
    def get(self) -> None:
        super().get()
        self.write('<link rel="stylesheet" href="/one-style.css"><div>hello, world!</div>')


class CSSRedirectHandler1(BaseHandler):
    def get(self) -> None:
        super().get()
        self.redirect('/two-style.css')


class CSSRedirectHandler2(BaseHandler):
    def get(self) -> None:
        super().get()
        self.redirect('/three-style.css')


class CSSRedirectHandler3(BaseHandler):
    def get(self) -> None:
        super().get()
        self.redirect('/four-style.css')


class CSSRedirectHandler4(BaseHandler):
    def get(self) -> None:
        super().get()
        self.write('body {box-sizing: border-box;}')


class CSPHandler(BaseHandler):
    def get(self) -> None:
        super().get()
        self.set_header('Content-Security-Policy', 'script-src \'self\'')
        self.write('')


def auth_api(username: str, password: str) -> bool:
    if username == 'user' and password == 'pass':
        return True
    else:
        return False


def basic_auth(auth: Callable[[str, str], bool]) -> Callable:
    def wrapper(f: Callable) -> Callable:
        def _request_auth(handler: Any) -> None:
            handler.set_header('WWW-Authenticate', 'Basic realm=JSL')
            handler.set_status(401)
            handler.finish()

        @functools.wraps(f)
        def new_f(*args: Any) -> None:
            handler = args[0]

            auth_header = handler.request.headers.get('Authorization')
            if auth_header is None:
                return _request_auth(handler)
            if not auth_header.startswith('Basic '):
                return _request_auth(handler)

            auth_decoded = base64.b64decode(auth_header[6:])
            username, password = auth_decoded.decode('utf-8').split(':', 2)

            if auth(username, password):
                f(*args)
            else:
                _request_auth(handler)

        return new_f

    return wrapper


class AuthHandler(BaseHandler):
    @basic_auth(auth_api)
    def get(self) -> None:
        super().get()
        self.write('ok')


def log_handler(handler: Any) -> None:
    """Override tornado's logging."""
    # log only errors (status >= 500)
    if handler.get_status() >= 500:
        access_log.error('{} {}'.format(handler.get_status(), handler._request_summary()))


class _StaticFileHandler(web.StaticFileHandler):
    # todo: feels like a hack...
    request_headers = {}
    one_time_request_headers = {}
    callbacks = {}
    request_preconditions = {}

    @classmethod
    def set_request_header(cls, path: str, headers: Dict[str, str], one_time: bool = False):
        (cls.one_time_request_headers if one_time else cls.request_headers)[path.strip('/')] = headers

    @classmethod
    def add_one_time_callback(cls, path: str, func: Callable[[HTTPServerRequest], Any]):
        cls.callbacks[path.strip('/')] = func

    @classmethod
    def add_request_precondition(cls, path: str, precondition: Union[Awaitable, Callable[[], None]]):
        cls.request_preconditions[path.strip('/')] = precondition

    async def get(self, path: str, include_body: bool = True) -> None:
        if path in self.callbacks:
            self.callbacks[path](self.request)
            del self.callbacks[path]
        if path in self.request_preconditions:
            func = self.request_preconditions[path]
            del self.request_preconditions[path]
            func_res = func()
            if isawaitable(func_res):
                await func_res
        headers = self.request_headers.get(path, {})
        if path in self.one_time_request_headers:
            headers = self.one_time_request_headers[path]
            del self.one_time_request_headers[path]

        if headers:
            [self.set_header(k, v) for k, v in headers.items()]
        await super().get(path, include_body)


class _Application(web.Application):
    def __init__(
        self,
        handlers: _RuleList = None,
        default_host: str = None,
        transforms: List[Type["OutputTransform"]] = None,
        **settings: Any,
    ) -> None:
        self._handlers = handlers
        super().__init__(handlers, default_host, transforms, **settings)

    def add_request_precondition(self, path: str, precondition: Union[Awaitable, Callable[[], None]]):
        self._handlers[0][1].add_request_precondition(urlparse(path).path, precondition)

    def add_one_time_header_for_request(self, path: str, headers: Dict[str, str]):
        self._handlers[0][1].set_request_header(urlparse(path).path, headers, True)

    def add_header_for_request(self, path: str, headers: Dict[str, str]):
        self._handlers[0][1].set_request_header(urlparse(path).path, headers, False)

    def waitForRequest(self, path: str) -> Awaitable[HTTPServerRequest]:
        fut = asyncio.get_event_loop().create_future()

        def resolve_fut(req):
            fut.set_result(req)

        self._handlers[0][1].add_one_time_callback(urlparse(path).path, resolve_fut)
        return fut


def get_application() -> _Application:
    static_path = Path(__file__).parent / 'assets'
    handlers = [
        (r'/(.*)', _StaticFileHandler, {'path': static_path.name}),
    ]
    return _Application(handlers, log_function=log_handler, static_path=static_path,)


if __name__ == '__main__':
    app = get_application()
    port = get_free_port()
    app.listen(port)
    print(f'server running on http://localhost:{port}')
    asyncio.get_event_loop().run_forever()
