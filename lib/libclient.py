import io
import json
import selectors
import struct
import sys


class Message:
    def __init__(self, selector, sock, addr, request=None):
        self.selector = selector
        self.sock = sock
        self.addr = addr
        self.request = request
        self._recv_buffer = b""
        self._send_buffer = b""
        self._request_queued = False
        self._jsonheader_len = None
        self.jsonheader = None
        self.response = None
        self.msgToPrint = ""

    def _set_selector_events_mask(self, mode):
        """Set selector to listen for events: mode is 'r', 'w', or 'rw'."""
        if mode == "r":
            events = selectors.EVENT_READ
        elif mode == "w":
            events = selectors.EVENT_WRITE
        elif mode == "rw":
            events = selectors.EVENT_READ | selectors.EVENT_WRITE
        else:
            raise ValueError(f"Invalid events mask mode {mode!r}.")
        self.selector.modify(self.sock, events, data=self)

    def _read(self):
        try:
            # Should be ready to read
            data = self.sock.recv(4096)
        except BlockingIOError:
            # Resource temporarily unavailable (errno EWOULDBLOCK)
            pass
        else:
            if data:
                self._recv_buffer += data
            else:
                raise RuntimeError("Peer closed.")

    def _write(self):
        if self._send_buffer:
            print(f"Sending {self._send_buffer!r} to {self.addr}\n")
            try:
                # Should be ready to write
                sent = self.sock.send(self._send_buffer)
            except BlockingIOError:
                # Resource temporarily unavailable (errno EWOULDBLOCK)
                pass
            else:
                self._send_buffer = self._send_buffer[sent:]
                print(self.msgToPrint)
                if not self._send_buffer:
                    self._request_queued = False
                    self._set_selector_events_mask("r")


    def _json_encode(self, obj, encoding):
        return json.dumps(obj, ensure_ascii=False).encode(encoding)

    def _json_decode(self, json_bytes, encoding):
        tiow = io.TextIOWrapper(
            io.BytesIO(json_bytes), encoding=encoding, newline=""
        )
        obj = json.load(tiow)
        tiow.close()
        return obj

    def _create_message(
        self, *, content_bytes, content_type, content_encoding
    ):
        jsonheader = {
            "byteorder": sys.byteorder,
            "content-type": content_type,
            "content-encoding": content_encoding,
            "content-length": len(content_bytes),
        }
        jsonheader_bytes = self._json_encode(jsonheader, "utf-8")
        message_hdr = struct.pack(">H", len(jsonheader_bytes))
        message = message_hdr + jsonheader_bytes + content_bytes
        self.msgToPrint = f"""
        Header Length: {len(jsonheader_bytes)}
        [Header]
        - Byte-order: {sys.byteorder}
        - Content-type: {content_type}
        - Content-encoding: {content_encoding}
        - Content-Length: {len(content_bytes)}
        [Body]
        - {self.request["content"]}
        """
        return message

    def _create_request(self):
        msg = '''
        help - display this message
        search - search for a value
        exit - exit the program
        '''
        print(msg)
        while True:
            inp = str(input(">>> "))
            if inp == "help":
                print(msg)
            elif inp == "search":
                val = str(input(">>> "))
                self.request = dict(
                    type = "text/json",
                    encoding = "utf-8",
                    content = dict(action = "search", value = val)
                )
                break
            elif inp == "exit":
                sys.exit(0)

    def process_events(self, mask):
        if mask & selectors.EVENT_READ:
            self.read()
        if mask & selectors.EVENT_WRITE:
            self.write()

    def read(self):
        self._read()

        if self._jsonheader_len is None:
            self.process_protoheader()

        if self._jsonheader_len is not None:
            if self.jsonheader is None:
                self.process_jsonheader()

        if self.jsonheader:
            if self.response is None:
                self.process_response()

    def write(self):
        if not self._request_queued:
            self._create_request()
            self.queue_request()

        self._write()

    def close(self):
        print(f"Closing connection to {self.addr}\n")
        try:
            self.selector.unregister(self.sock)
        except Exception as e:
            print(
                f"Error: selector.unregister() exception for {self.addr}: {e!r}\n"
            )

        try:
            self.sock.close()
        except OSError as e:
            print(f"Error: socket.close() exception for {self.addr}: {e!r}\n")
        finally:
            # Delete reference to socket object for garbage collection
            self.sock = None

    def queue_request(self):
        content = self.request["content"]
        content_type = self.request["type"]
        content_encoding = self.request["encoding"]
        if content_type == "text/json":
            req = {
                "content_bytes": self._json_encode(content, content_encoding),
                "content_type": content_type,
                "content_encoding": content_encoding,
            }
            message = self._create_message(**req)
            self._send_buffer += message
            self._request_queued = True

    def process_protoheader(self):
        hdrlen = 2
        if len(self._recv_buffer) >= hdrlen:
            self._jsonheader_len = struct.unpack(
                ">H", self._recv_buffer[:hdrlen]
            )[0]
            self._recv_buffer = self._recv_buffer[hdrlen:]

    def process_jsonheader(self):
        hdrlen = self._jsonheader_len
        if len(self._recv_buffer) >= hdrlen:
            self.jsonheader = self._json_decode(
                self._recv_buffer[:hdrlen], "utf-8"
            )
            self._recv_buffer = self._recv_buffer[hdrlen:]
            for reqhdr in (
                "byteorder",
                "content-length",
                "content-type",
                "content-encoding",
            ):
                if reqhdr not in self.jsonheader:
                    raise ValueError(f"Missing required header '{reqhdr}'.")

    def process_response(self):
        content_len = self.jsonheader["content-length"]
        if not len(self._recv_buffer) >= content_len:
            return
        data = self._recv_buffer[:content_len]
        self._recv_buffer = self._recv_buffer[content_len:]
        if self.jsonheader["content-type"] == "text/json":
            encoding = self.jsonheader["content-encoding"]
            self.response = self._json_decode(data, encoding)
            message = f'''\n
            Header Length: [{self._jsonheader_len}] bytes
            [Header begin]
            - Byte Order: {self.jsonheader["byteorder"]}
            - Content Type: {self.jsonheader["content-type"]}
            - Content Encoding: {self.jsonheader["content-encoding"]}
            - Content Length: {self.jsonheader["content-length"]}
            [Header end]
            [Body begin]
            - Result: {self.response["result"]}
            [Body end]\n
            '''
            print(message)

            self._jsonheader_len = None
            self.jsonheader = None
            self.response = None

            # Listen for writes again
            self._set_selector_events_mask("w")

'''
Program Flow:
Process events is called
    |
Read:
1. Calls _read: reads data
2. Have three checks to make sure the whole packet is delivered. We extract
   Header lenght, Header and the actual request
    |
Write:
same as libserver
'''