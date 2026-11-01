"""Client for the IQ router stats endpoint (``--stats-port``, default 7001).

Line protocol like ``:5002``: one command per connection, one JSON object
followed by "\\n"; ``AUTH <token> `` prefix when a token is configured.
Verbs: PING, VERSION, STATS, SOURCES, CLIENTS (spec E.6)."""
from .ctl import QUERY_TIMEOUT_SEC
from .federation import CONNECT_TIMEOUT_SEC, Reply, line_request, parse_json_reply


class RouterStatsClient:
    def __init__(self, host, port, auth_token=None, timeout=None):
        self.host = host
        self.port = port
        self.auth_token = auth_token
        self.timeout = timeout if timeout is not None else QUERY_TIMEOUT_SEC

    def request(self, verb):
        text = line_request(self.host, self.port, verb, None, self.auth_token,
                            CONNECT_TIMEOUT_SEC, self.timeout)
        return Reply(parse_json_reply(text, self.host, self.port), text.rstrip("\n"),
                     (self.host, self.port))

    def ping(self):
        return self.request("PING")

    def version(self):
        return self.request("VERSION")

    def stats(self):
        return self.request("STATS")

    def sources(self):
        return self.request("SOURCES")

    def clients(self):
        return self.request("CLIENTS")
