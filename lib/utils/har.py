#!/usr/bin/env python

"""
Copyright (c) 2006-2017 sqlmap developers (http://sqlmap.org/)
See the file 'doc/COPYING' for copying permission
"""

import base64
import BaseHTTPServer
import httplib
import re
import StringIO

from lib.core.data import logger
from lib.core.settings import VERSION

class HTTPCollectorFactory:
    def __init__(self, harFile=False):
        self.harFile = harFile

    def create(self):
        collector = HTTPCollector()

        return collector

class HTTPCollector:
    def __init__(self):
        self.messages = []

    def collectRequest(self, requestMessage, responseMessage):
        self.messages.append(RawPair(requestMessage, responseMessage))

    def obtain(self):
        return {"log": {
            "version": "1.2",
            "creator": {"name": "sqlmap", "version": VERSION},
            "entries": [pair.toEntry().toDict() for pair in self.messages],
        }}

class RawPair:
    def __init__(self, request, response):
        self.request = request
        self.response = response

    def toEntry(self):
        return Entry(request=Request.parse(self.request),
                     response=Response.parse(self.response))

class Entry:
    def __init__(self, request, response):
        self.request = request
        self.response = response

    def toDict(self):
        return {
            "request": self.request.toDict(),
            "response": self.response.toDict(),
        }

class Request:
    def __init__(self, method, path, httpVersion, headers, postBody=None, raw=None, comment=None):
        self.method = method
        self.path = path
        self.httpVersion = httpVersion
        self.headers = headers or {}
        self.postBody = postBody
        self.comment = comment
        self.raw = raw

    @classmethod
    def parse(cls, raw):
        request = HTTPRequest(raw)
        return cls(method=request.command,
                   path=request.path,
                   httpVersion=request.request_version,
                   headers=request.headers,
                   postBody=request.rfile.read(),
                   comment=request.comment,
                   raw=raw)

    @property
    def url(self):
        host = self.headers.get("Host", "unknown")
        return "http://%s%s" % (host, self.path)

    def toDict(self):
        out = {
            "httpVersion": self.httpVersion,
            "method": self.method,
            "url": self.url,
            "headers": [dict(name=key.capitalize(), value=value) for key, value in self.headers.items()],
            "comment": self.comment,
        }

        if self.postBody:
            contentType = self.headers.get("Content-Type")
            out["postData"] = {
                "mimeType": contentType,
                "text": self.postBody.rstrip("\r\n"),
            }

        return out

class Response:
    extract_status = re.compile(r'\((\d{3}) (.*)\)')

    def __init__(self, httpVersion, status, statusText, headers, content, raw=None, comment=None):
        self.raw = raw
        self.httpVersion = httpVersion
        self.status = status
        self.statusText = statusText
        self.headers = headers
        self.content = content
        self.comment = comment

    @classmethod
    def parse(cls, raw):
        altered = raw
        comment = None

        if altered.startswith("HTTP response ["):
            io = StringIO.StringIO(raw)
            first_line = io.readline()
            parts = cls.extract_status.search(first_line)
            status_line = "HTTP/1.0 %s %s" % (parts.group(1), parts.group(2))
            remain = io.read()
            altered = status_line + "\n" + remain
            comment = first_line

        response = httplib.HTTPResponse(FakeSocket(altered))
        response.begin()

        try:
            content = response.read(-1)
        except httplib.IncompleteRead:
            content = raw[raw.find("\n\n") + 2:].rstrip("\r\n")

        return cls(httpVersion="HTTP/1.1" if response.version == 11 else "HTTP/1.0",
                   status=response.status,
                   statusText=response.reason,
                   headers=response.msg,
                   content=content,
                   comment=comment,
                   raw=raw)

    def toDict(self):
        content = {
            "mimeType": self.headers.get("Content-Type"),
            "text": self.content,
        }

        binary = set(['\0', '\1'])
        if any(c in binary for c in self.content):
            content["encoding"] = "base64"
            content["text"] = base64.b64encode(self.content)

        return {
            "httpVersion": self.httpVersion,
            "status": self.status,
            "statusText": self.statusText,
            "headers": [dict(name=key.capitalize(), value=value) for key, value in self.headers.items() if key.lower() != "uri"],
            "content": content,
            "comment": self.comment,
        }

class FakeSocket:
    # Original source:
    # https://stackoverflow.com/questions/24728088/python-parse-http-response-string

    def __init__(self, response_text):
        self._file = StringIO.StringIO(response_text)

    def makefile(self, *args, **kwargs):
        return self._file

class HTTPRequest(BaseHTTPServer.BaseHTTPRequestHandler):
    # Original source:
    # https://stackoverflow.com/questions/4685217/parse-raw-http-headers

    def __init__(self, request_text):
        self.comment = None
        self.rfile = StringIO.StringIO(request_text)
        self.raw_requestline = self.rfile.readline()

        if self.raw_requestline.startswith("HTTP request ["):
            self.comment = self.raw_requestline
            self.raw_requestline = self.rfile.readline()

        self.error_code = self.error_message = None
        self.parse_request()

    def send_error(self, code, message):
        self.error_code = code
        self.error_message = message
