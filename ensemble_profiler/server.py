import asyncio
import json

import uvicorn

import ray
from ray.experimental.async_api import _async_init
from ray.experimental.serve.constants import (HTTP_ROUTER_CHECKER_INTERVAL_S,
                                              PREDICATE_DEFAULT_VALUE,
                                              SERVE_PROFILE_PATH)
from ray.experimental.serve.context import TaskContext
from ray.experimental.serve.utils import BytesEncoder
from urllib.parse import parse_qs
from ray.experimental.serve.request_params import RequestParams
import os
from ray import cloudpickle as pickle
from ray.experimental.serve.http_util import build_flask_request
import time


class JSONResponse:
    """ASGI compliant response class.

    It is expected to be called in async context and pass along
    `scope, receive, send` as in ASGI spec.

    >>> await JSONResponse({"k": "v"})(scope, receive, send)
    """

    def __init__(self, content=None, status_code=200):
        """Construct a JSON HTTP Response.

        Args:
            content (optional): Any JSON serializable object.
            status_code (int, optional): Default status code is 200.
        """
        self.body = self.render(content)
        self.status_code = status_code
        self.raw_headers = [[b"content-type", b"application/json"]]

    def render(self, content):
        if content is None:
            return b""
        if isinstance(content, bytes):
            return content
        return json.dumps(content, cls=BytesEncoder, indent=2).encode()

    async def __call__(self, scope, receive, send):
        await send({
            "type": "http.response.start",
            "status": self.status_code,
            "headers": self.raw_headers,
        })
        await send({"type": "http.response.body", "body": self.body})


class HTTPProxy:
    """
    This class should be instantiated and ran by ASGI server.

    >>> import uvicorn
    >>> uvicorn.run(HTTPProxy(kv_store_actor_handle, router_handle))
    # blocks forever
    """

    def __init__(self, address, pipeline):
        assert ray.is_initialized()
        self.route_checker_should_shutdown = False
        self.address = address 
        self.pipeline = pipeline
        self.profile_file = open(
            os.environ.get(
                "ENSEMBLE_PROFILE_PATH", "/tmp/ensemble_profile.jsonl"),"w")

    async def handle_lifespan_message(self, scope, receive, send):
        assert scope["type"] == "lifespan"

        message = await receive()
        if message["type"] == "lifespan.startup":
            await _async_init()
            await send({"type": "lifespan.startup.complete"})
        elif message["type"] == "lifespan.shutdown":
            self.route_checker_should_shutdown = True
            await send({"type": "lifespan.shutdown.complete"})

    async def receive_http_body(self, scope, receive, send):
        body_buffer = []
        more_body = True
        while more_body:
            message = await receive()
            assert message["type"] == "http.request"

            more_body = message["more_body"]
            body_buffer.append(message["body"])

        return b"".join(body_buffer)

    

    async def __call__(self, scope, receive, send):
        # NOTE: This implements ASGI protocol specified in
        #       https://asgi.readthedocs.io/en/latest/specs/index.html

        if scope["type"] == "lifespan":
            await self.handle_lifespan_message(scope, receive, send)
            return

        assert scope["type"] == "http"
        current_path = scope["path"]
        if current_path == "/":
            await JSONResponse(self.route_table_cache)(scope, receive, send)
            return

        # TODO(simon): Use werkzeug route mapper to support variable path
        if current_path != self.address:
            error_message = ("Path {} not found. "
                             "Please ping http://.../ for routing table"
                             ).format(current_path)
            await JSONResponse(
                {
                    "error": error_message
                }, status_code=404)(scope, receive, send)
            return


        http_body_bytes = await self.receive_http_body(scope, receive, send)

        # get slo_ms before enqueuing the query
        query_string = scope["query_string"].decode("ascii")
        query_kwargs = parse_qs(query_string)
        request_slo_ms = query_kwargs.pop("slo_ms", None)
        if request_slo_ms is not None:
            try:
                if len(request_slo_ms) != 1:
                    raise ValueError(
                        "Multiple SLO specified, please specific only one.")
                request_slo_ms = request_slo_ms[0]
                request_slo_ms = float(request_slo_ms)
                if request_slo_ms < 0:
                    raise ValueError(
                        "Request SLO must be positive, it is {}".format(
                            request_slo_ms))
            except ValueError as e:
                await JSONResponse({"error": str(e)})(scope, receive, send)
                return


        # TODO(alind): File a Ray issue if args contain b"" it is not
        #              received.
        # Hence enclosing http_body_bytes inside a list.
        # args = (scope, [http_body_bytes])
        # kwargs = dict()
        flask_request = build_flask_request(scope,http_body_bytes)
        info = {
        "patient_name" : flask_request.args.get("patient_name"),
        "value" : flask_request.args.get("value"),
        "vtype" : flask_request.args.get("vtype")
        }

        request_sent_time = time.time()
        # await for request info to get back
        req_info = await self.pipeline.remote(info=info)

        # await for result
        result = await next(iter(req_info))
        result_received_time = time.time()
        self.profile_file.write(
            json.dumps({
                "start": request_sent_time,
                "end": result_received_time
            }))
        self.profile_file.write("\n")
        self.profile_file.flush()

        if isinstance(result, ray.exceptions.RayTaskError):
            await JSONResponse({
                "error": "internal error, please use python API to debug"
            })(scope, receive, send)
        else:
            await JSONResponse({"result": result})(scope, receive, send)


@ray.remote
class HTTPActor:
    def __init__(self, address, pipeline):
        self.app = HTTPProxy(address, pipeline)

    def run(self, host="0.0.0.0", port=5000):
        uvicorn.run(
            self.app, host=host, port=port, lifespan="on", access_log=False)