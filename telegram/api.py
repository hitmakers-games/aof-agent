"""Telegram HTTP client. Never log exception URLs: they contain the token."""
import json
import re
import uuid
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class TelegramError(Exception):
    def __init__(self, code=0, retry_after=5, not_modified=False):
        super().__init__("Telegram API request failed")
        self.code = code
        self.retry_after = max(1, min(int(retry_after), 60))
        self.not_modified = not_modified


class Telegram:
    def __init__(self, token):
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{20,}", token):
            raise ValueError("Invalid Telegram token format")
        self.token = token

    def call(self, method, _timeout=40, _upload=None, **payload):
        body=json.dumps(payload).encode()
        content_type="application/json"
        if _upload is not None:
            field,filename,content=_upload
            boundary=uuid.uuid4().hex
            filename=re.sub(r'[^A-Za-z0-9._-]',"_",filename)[:200]
            parts=[]
            for key,value in payload.items():
                parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n').encode())
            parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n').encode()+content+b"\r\n")
            parts.append((f"--{boundary}--\r\n").encode())
            body=b"".join(parts)
            content_type="multipart/form-data; boundary="+boundary
        request = Request(
            f"https://api.telegram.org/bot{self.token}/{method}",
            data=body,
            headers={"Content-Type": content_type},
        )
        try:
            with urlopen(request, timeout=_timeout) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                raise TelegramError()
            data = json.loads(raw)
        except HTTPError as error:
            delay = 5
            unchanged = False
            try:
                data = json.loads(error.read(8192))
                delay = data.get("parameters", {}).get("retry_after", 5)
                unchanged = 'message is not modified' in str(data.get('description', '')).lower()
            except (ValueError, OSError, AttributeError):
                pass
            raise TelegramError(error.code, delay, unchanged) from None
        except (URLError, OSError, ValueError):
            # HTTP exceptions contain the bot token in their URL. Never log them.
            raise TelegramError() from None
        if not isinstance(data, dict) or not data.get("ok"):
            raise TelegramError()
        return data["result"]
