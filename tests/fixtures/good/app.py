from sse_starlette.sse import EventSourceResponse
response = EventSourceResponse(gen)
