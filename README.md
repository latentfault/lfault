# lfault

An HTTP proxy with boundary issues.

The proxy forwards original HTTP bytes, handling one exchange per connection
or switching to a bidirectional tunnel for CONNECT and protocol upgrades.

* `lfault/server.py` coordinates exchanges, upstream connections, and diagnostics.
* `lfault/http1.py` interprets request routes and HTTP framing, and relays framed bodies.
* `lfault/streams.py` owns unread-byte buffers and generic byte relays. Callers own
  socket lifetimes; tunnel relaying flushes buffered bytes before reading sockets directly.

## Run

```console
python3 -m lfault
```

## Test

```console
python3 -m unittest -v
```
