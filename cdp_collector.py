from __future__ import annotations

import base64
import json
import os
import re
import socket
import struct
import subprocess
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlparse


BASE_DIR = Path(__file__).resolve().parent
RESULTS_DIR = BASE_DIR / "yingdao_results"
DEEPSEEK_URL = "https://chat.deepseek.com/"
CDP_HOST = "127.0.0.1"
CDP_PORT = 9222
ANSWER_TIMEOUT_SECONDS = int(os.getenv("YINGDAO_ANSWER_TIMEOUT_SECONDS", "150"))
DESKTOP_CAPTURE_MAX_PAGES = 20
CDP_BRING_TO_FRONT = os.getenv("YINGDAO_BRING_TO_FRONT", "0").strip() == "1"

GATE_MARKERS = (
    "验证码",
    "安全验证",
    "安全检测",
    "人机验证",
    "请完成验证",
    "访问受限",
    "captcha",
    "verify",
)


def run_deepseek(task: dict) -> dict:
    screenshot_path, answer_path, url_path = _make_paths(task)
    if not _cdp_is_available():
        screenshot = _desktop_screenshot(screenshot_path)
        return {
            "status": "failed",
            "screenshot_path": screenshot,
            "answer_text_path": "",
            "answer_url": "",
            "url_text_path": "",
            "remark": "cdp_not_available_请先运行 yingdao_mvp/start_chrome_cdp.command",
        }
    return _run_deepseek_cdp(task, screenshot_path, answer_path, url_path)


def _run_deepseek_cdp(task: dict, screenshot_path: Path, answer_path: Path, url_path: Path) -> dict:
    try:
        _cdp_open_deepseek(fresh=True)
    except Exception as exc:
        screenshot = _desktop_screenshot(screenshot_path)
        return {
            "status": "failed",
            "screenshot_path": screenshot,
            "answer_text_path": "",
            "answer_url": "",
            "url_text_path": "",
            "remark": _short_remark(f"cdp_open_failed: {exc}"),
        }

    time.sleep(3)
    gate_reason = _cdp_gate_reason()
    if gate_reason:
        screenshot = _cdp_capture_page_screenshot(screenshot_path)
        answer_url = _cdp_current_url()
        url_text_path = _write_url(url_path, answer_url)
        return {
            "status": "manual_required",
            "screenshot_path": screenshot,
            "answer_text_path": "",
            "answer_url": answer_url,
            "url_text_path": url_text_path,
            "remark": f"DeepSeek 触发登录/验证/风控: {gate_reason}",
        }

    if not _cdp_submit_question(str(task.get("question") or "")):
        screenshot = _cdp_capture_page_screenshot(screenshot_path)
        answer_url = _cdp_current_url()
        url_text_path = _write_url(url_path, answer_url)
        return {
            "status": "failed",
            "screenshot_path": screenshot,
            "answer_text_path": "",
            "answer_url": answer_url,
            "url_text_path": url_text_path,
            "remark": "cdp_input_not_found_or_send_failed",
        }

    time.sleep(2)
    gate_reason = _cdp_gate_reason()
    if gate_reason:
        screenshot = _cdp_capture_page_screenshot(screenshot_path)
        answer_url = _cdp_current_url()
        url_text_path = _write_url(url_path, answer_url)
        return {
            "status": "manual_required",
            "screenshot_path": screenshot,
            "answer_text_path": "",
            "answer_url": answer_url,
            "url_text_path": url_text_path,
            "remark": f"DeepSeek 触发登录/验证/风控: {gate_reason}",
        }

    answer_text = _cdp_wait_answer()
    answer_url = _cdp_current_url()
    try:
        screenshot, extracted_text, screenshot_mode = _cdp_capture_deepseek_evidence(
            screenshot_path, str(task.get("question") or "")
        )
        if extracted_text:
            answer_text = extracted_text
    except Exception as exc:
        screenshot = _cdp_capture_page_screenshot(screenshot_path)
        answer_text_path = _write_answer(answer_path, answer_text)
        url_text_path = _write_url(url_path, answer_url)
        return {
            "status": "failed",
            "screenshot_path": screenshot,
            "answer_text_path": answer_text_path,
            "answer_url": answer_url,
            "url_text_path": url_text_path,
            "remark": _short_remark(f"cdp_screenshot_failed: {exc}"),
        }

    answer_text_path = _write_answer(answer_path, answer_text)
    url_text_path = _write_url(url_path, answer_url)
    return {
        "status": "success" if answer_text else "failed",
        "screenshot_path": screenshot,
        "answer_text_path": answer_text_path,
        "answer_url": answer_url,
        "url_text_path": url_text_path,
        "remark": (
            f"正常完成_{screenshot_mode}"
            if answer_text
            else f"timeout_waiting_answer_{screenshot_mode}_未提取文本"
        ),
    }


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _time_tag() -> str:
    return datetime.now().strftime("%H%M%S")


def _date_tag() -> str:
    return datetime.now().strftime("%Y%m%d")


def _short_remark(message: str, limit: int = 500) -> str:
    message = str(message).replace("\r", " ").strip()
    return message if len(message) <= limit else message[:limit] + "..."


def _safe_path_part(value: str, limit: int = 90) -> str:
    safe = re.sub(r"[\\/:*?\"<>|\r\n\t]+", " ", str(value)).strip()
    safe = re.sub(r"\s+", " ", safe)
    safe = safe.rstrip(".")
    if not safe:
        safe = "未命名问题"
    return safe[:limit].strip() or "未命名问题"


def _make_paths(task: dict) -> tuple[Path, Path, Path]:
    question_folder = _safe_path_part(task.get("question") or task.get("id") or f"row_{_time_tag()}")
    output_dir = RESULTS_DIR / _date_tag() / question_folder
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_id = re.sub(r"[^A-Za-z0-9_-]+", "_", str(task.get("id") or f"row_{_time_tag()}"))
    platform = str(task.get("platform") or "deepseek")
    round_value = str(task.get("round") or "1")
    base = f"{safe_id}_{platform}_round{round_value}_{_time_tag()}"
    return output_dir / f"{base}.png", output_dir / f"{base}.txt", output_dir / f"{base}_url.txt"


def _write_answer(answer_path: Path, answer_text: str) -> str:
    answer_path.parent.mkdir(parents=True, exist_ok=True)
    answer_path.write_text(answer_text or "", encoding="utf-8")
    return str(answer_path)


def _write_url(url_path: Path, answer_url: str) -> str:
    url_path.parent.mkdir(parents=True, exist_ok=True)
    url_path.write_text(answer_url or "", encoding="utf-8")
    return str(url_path)


class _CdpWebSocket:
    def __init__(self, ws_url: str, timeout: int = 20):
        parsed = urlparse(ws_url)
        self.host = parsed.hostname or CDP_HOST
        self.port = parsed.port or CDP_PORT
        self.path = parsed.path
        if parsed.query:
            self.path += "?" + parsed.query
        self.timeout = timeout
        self.sock: socket.socket | None = None
        self.next_id = 1

    def __enter__(self):
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        self.sock.sendall(request.encode("ascii"))
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            response += chunk
        if b" 101 " not in response.split(b"\r\n", 1)[0]:
            raise RuntimeError(f"cdp_websocket_upgrade_failed: {response[:200]!r}")
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
        self.sock = None

    def _recv_exact(self, size: int) -> bytes:
        assert self.sock is not None
        chunks = []
        remaining = size
        while remaining:
            chunk = self.sock.recv(remaining)
            if not chunk:
                raise RuntimeError("cdp_websocket_closed")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        assert self.sock is not None
        first = 0x80 | opcode
        length = len(payload)
        if length < 126:
            header = struct.pack("!BB", first, 0x80 | length)
        elif length < 65536:
            header = struct.pack("!BBH", first, 0x80 | 126, length)
        else:
            header = struct.pack("!BBQ", first, 0x80 | 127, length)
        mask = os.urandom(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def _recv_message(self) -> str:
        message = bytearray()
        while True:
            header = self._recv_exact(2)
            first, second = header[0], header[1]
            fin = bool(first & 0x80)
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if masked else b""
            payload = self._recv_exact(length) if length else b""
            if masked:
                payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
            if opcode == 8:
                raise RuntimeError("cdp_websocket_closed")
            if opcode == 9:
                self._send_frame(10, payload)
                continue
            if opcode in (1, 0):
                message.extend(payload)
                if fin:
                    return message.decode("utf-8")

    def send_cmd(self, method: str, params: dict | None = None) -> dict:
        msg_id = self.next_id
        self.next_id += 1
        self._send_frame(1, json.dumps({"id": msg_id, "method": method, "params": params or {}}).encode("utf-8"))
        while True:
            raw = self._recv_message()
            message = json.loads(raw)
            if message.get("id") != msg_id:
                continue
            if "error" in message:
                raise RuntimeError(f"cdp_error_{method}: {message['error']}")
            return message.get("result") or {}


def _cdp_json(path: str, timeout: int = 2, method: str = "GET"):
    url = f"http://{CDP_HOST}:{CDP_PORT}{path}"
    request = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _cdp_is_available() -> bool:
    try:
        _cdp_json("/json/version")
        return True
    except Exception:
        return False


def _cdp_targets() -> list[dict]:
    try:
        targets = _cdp_json("/json/list")
    except Exception:
        return []
    return targets if isinstance(targets, list) else []


def _cdp_deepseek_ws_url(open_if_missing: bool = False) -> str | None:
    targets = _cdp_targets()
    for target in targets:
        url = target.get("url") or ""
        if "chat.deepseek.com" in url and target.get("webSocketDebuggerUrl"):
            return target["webSocketDebuggerUrl"]
    if open_if_missing:
        try:
            created = _cdp_json(f"/json/new?{quote(DEEPSEEK_URL, safe='')}", method="PUT")
            if created.get("webSocketDebuggerUrl"):
                return created["webSocketDebuggerUrl"]
        except Exception:
            pass
    return None


def _cdp_close_target(target_id: str) -> None:
    try:
        url = f"http://{CDP_HOST}:{CDP_PORT}/json/close/{quote(target_id, safe='')}"
        with urllib.request.urlopen(url, timeout=3) as response:
            response.read()
    except Exception:
        pass


def _cdp_close_deepseek_pages() -> None:
    for target in _cdp_targets():
        if target.get("type") != "page":
            continue
        if "chat.deepseek.com" not in (target.get("url") or ""):
            continue
        target_id = target.get("id")
        if target_id:
            _cdp_close_target(target_id)
    time.sleep(0.8)


def _cdp_new_deepseek_page() -> str | None:
    for encoded_url in (quote(DEEPSEEK_URL, safe=""), DEEPSEEK_URL):
        try:
            created = _cdp_json(f"/json/new?{encoded_url}", timeout=5, method="PUT")
            if created.get("webSocketDebuggerUrl"):
                return created["webSocketDebuggerUrl"]
        except Exception:
            pass
    return None


def _cdp_runtime_value(result: dict):
    payload = result.get("result") or {}
    if "value" in payload:
        return payload.get("value")
    if payload.get("type") == "undefined":
        return None
    return payload.get("description")


def _cdp_evaluate(cdp: _CdpWebSocket, expression: str, await_promise: bool = True):
    result = cdp.send_cmd(
        "Runtime.evaluate",
        {"expression": expression, "returnByValue": True, "awaitPromise": await_promise},
    )
    return _cdp_runtime_value(result)


def _cdp_bring_to_front(cdp: _CdpWebSocket) -> None:
    if CDP_BRING_TO_FRONT:
        cdp.send_cmd("Page.bringToFront")


def _cdp_open_deepseek(fresh: bool = False) -> str:
    if fresh:
        _cdp_close_deepseek_pages()
        ws_url = _cdp_new_deepseek_page()
    else:
        ws_url = _cdp_deepseek_ws_url(open_if_missing=True)
    if not ws_url:
        raise RuntimeError("cdp_not_available_请先运行 yingdao_mvp/start_chrome_cdp.command")
    with _CdpWebSocket(ws_url) as cdp:
        cdp.send_cmd("Page.enable")
        cdp.send_cmd("Runtime.enable")
        _cdp_bring_to_front(cdp)
        current_url = str(_cdp_evaluate(cdp, "location.href", await_promise=False) or "")
        if "chat.deepseek.com" not in current_url:
            cdp.send_cmd("Page.navigate", {"url": DEEPSEEK_URL})
            time.sleep(5)
    return ws_url


def _cdp_page_text() -> str:
    ws_url = _cdp_deepseek_ws_url()
    if not ws_url:
        return ""
    try:
        with _CdpWebSocket(ws_url) as cdp:
            cdp.send_cmd("Runtime.enable")
            return str(_cdp_evaluate(cdp, "document.body ? document.body.innerText : ''") or "")
    except Exception:
        return ""


def _cdp_current_url() -> str:
    ws_url = _cdp_deepseek_ws_url()
    if not ws_url:
        return ""
    try:
        with _CdpWebSocket(ws_url) as cdp:
            cdp.send_cmd("Runtime.enable")
            return str(_cdp_evaluate(cdp, "location.href", await_promise=False) or "")
    except Exception:
        return ""


def _cdp_gate_reason() -> str | None:
    url = _cdp_current_url().lower()
    if "/sign" in url or "/login" in url:
        return "login_url"
    lower = _cdp_page_text().lower()
    for marker in GATE_MARKERS:
        if marker.lower() in lower:
            return marker
    return None


def _cdp_submit_question(question: str) -> bool:
    ws_url = _cdp_deepseek_ws_url()
    if not ws_url:
        return False

    focus_expression = r"""
    (() => {
      const visible = el => {
        const r = el.getBoundingClientRect();
        const s = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
      };
      const candidates = Array.from(document.querySelectorAll(
        'textarea,[contenteditable="true"],[role="textbox"],.ProseMirror'
      )).filter(visible);
      const input = candidates[candidates.length - 1];
      if (!input) return JSON.stringify({ ok: false, reason: 'input_not_found' });
      input.scrollIntoView({ block: 'center', inline: 'nearest' });
      input.focus();
      if (input.tagName === 'TEXTAREA' || input.tagName === 'INPUT') {
        const setter = Object.getOwnPropertyDescriptor(input.constructor.prototype, 'value')?.set;
        if (setter) setter.call(input, '');
        else input.value = '';
      } else {
        input.textContent = '';
      }
      input.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'deleteContentBackward', data: null }));
      return JSON.stringify({ ok: true });
    })()
    """
    click_send_expression = r"""
    (() => {
      const visible = el => {
        const r = el.getBoundingClientRect();
        const s = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
      };
      const inputs = Array.from(document.querySelectorAll(
        'textarea,[contenteditable="true"],[role="textbox"],.ProseMirror'
      )).filter(visible);
      const input = inputs[inputs.length - 1];
      const inputRect = input ? input.getBoundingClientRect() : null;
      const buttons = Array.from(document.querySelectorAll('button,[role="button"]'))
        .filter(el => visible(el) && !el.disabled && el.getAttribute('aria-disabled') !== 'true');
      let send = buttons.find(el => /发送|send|提交|submit/i.test((el.innerText || el.ariaLabel || el.title || '').trim()));
      if (!send && inputRect) {
        const nearInput = buttons.filter(el => {
          const r = el.getBoundingClientRect();
          return r.top >= inputRect.top - 80
            && r.bottom <= inputRect.bottom + 80
            && r.left >= inputRect.right - 260;
        });
        nearInput.sort((a, b) => {
          const ar = a.getBoundingClientRect();
          const br = b.getBoundingClientRect();
          return (br.right + br.bottom) - (ar.right + ar.bottom);
        });
        send = nearInput[0];
      }
      if (!send) return JSON.stringify({ ok: false, reason: 'send_not_found' });
      send.click();
      return JSON.stringify({ ok: true });
    })()
    """
    try:
        with _CdpWebSocket(ws_url) as cdp:
            cdp.send_cmd("Runtime.enable")
            cdp.send_cmd("Input.setIgnoreInputEvents", {"ignore": False})
            focus_info = json.loads(_cdp_evaluate(cdp, focus_expression) or "{}")
            if not focus_info.get("ok"):
                return False
            cdp.send_cmd("Input.insertText", {"text": question})
            time.sleep(0.8)
            send_info = json.loads(_cdp_evaluate(cdp, click_send_expression) or "{}")
            if send_info.get("ok"):
                return True
            cdp.send_cmd(
                "Input.dispatchKeyEvent",
                {"type": "keyDown", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13},
            )
            cdp.send_cmd(
                "Input.dispatchKeyEvent",
                {"type": "keyUp", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13},
            )
            return True
    except Exception:
        return False


def _cdp_answer_state() -> dict:
    ws_url = _cdp_deepseek_ws_url()
    if not ws_url:
        return {"answerText": "", "generating": False}
    expression = r"""
    (() => {
      const visible = el => {
        const r = el.getBoundingClientRect();
        const s = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
      };
      const buttons = Array.from(document.querySelectorAll('button,[role="button"]')).filter(visible);
      const generating = buttons.some(el => /停止|停止生成|stop generating|stop/i.test((el.innerText || el.ariaLabel || el.title || '').trim()));
      const selectors = ['.ds-markdown','[class*="markdown"]','article','[class*="answer"]','[class*="message-content"]','[class*="message"]'];
      let texts = [];
      for (const selector of selectors) {
        const matches = [];
        for (const el of document.querySelectorAll(selector)) {
          const text = (el.innerText || '').trim();
          if (visible(el) && text.length > 0) matches.push(text);
        }
        if (matches.length) {
          texts = matches;
          break;
        }
      }
      texts = Array.from(new Set(texts));
      return JSON.stringify({ answerText: texts.length ? texts[texts.length - 1] : '', generating });
    })()
    """
    try:
        with _CdpWebSocket(ws_url) as cdp:
            cdp.send_cmd("Runtime.enable")
            return json.loads(_cdp_evaluate(cdp, expression) or "{}")
    except Exception:
        return {"answerText": "", "generating": False}


def _cdp_wait_answer(timeout_seconds: int = ANSWER_TIMEOUT_SECONDS) -> str:
    deadline = time.time() + timeout_seconds
    started_at = time.time()
    last_text = ""
    stable_since = time.time()
    best_text = ""
    while time.time() < deadline:
        state = _cdp_answer_state()
        text = str(state.get("answerText") or "").strip()
        generating = bool(state.get("generating"))
        if text:
            best_text = text
        if text != last_text:
            last_text = text
            stable_since = time.time()
        stable_seconds = time.time() - stable_since
        elapsed = time.time() - started_at
        if text and not generating and elapsed >= 8 and stable_seconds >= 5:
            return text
        time.sleep(2)
    return best_text


def _cdp_capture_page_screenshot(screenshot_path: Path) -> str:
    ws_url = _cdp_deepseek_ws_url()
    if not ws_url:
        return _desktop_screenshot(screenshot_path)
    screenshot_path.parent.mkdir(parents=True, exist_ok=True)
    with _CdpWebSocket(ws_url) as cdp:
        cdp.send_cmd("Page.enable")
        _cdp_bring_to_front(cdp)
        metrics = cdp.send_cmd("Page.getLayoutMetrics")
        content_size = metrics.get("contentSize") or {}
        width = max(1, int(content_size.get("width") or 1400))
        height = max(1, int(content_size.get("height") or 1000))
        data = cdp.send_cmd(
            "Page.captureScreenshot",
            {
                "format": "png",
                "fromSurface": False,
                "captureBeyondViewport": True,
                "clip": {"x": 0, "y": 0, "width": width, "height": height, "scale": 1},
            },
        )
    screenshot_path.write_bytes(base64.b64decode(data["data"]))
    _add_timestamp_to_image(screenshot_path)
    return str(screenshot_path)


def _cdp_enter_qa_capture_mode(cdp: _CdpWebSocket) -> None:
    expression = r"""
    (() => {
      document.querySelector('[data-geo-qa-capture-style="1"]')?.remove();
      document.querySelectorAll('[data-geo-qa-capture-hidden="1"]').forEach(el => el.removeAttribute('data-geo-qa-capture-hidden'));
      const viewportWidth = window.innerWidth || document.documentElement.clientWidth;
      const viewportHeight = window.innerHeight || document.documentElement.clientHeight;
      const visible = el => {
        const r = el.getBoundingClientRect();
        const s = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
      };
      const hide = el => {
        if (el && el !== document.body && el !== document.documentElement) {
          el.setAttribute('data-geo-qa-capture-hidden', '1');
        }
      };
      for (const el of Array.from(document.querySelectorAll('aside, nav, [class*="sidebar"], [class*="sider"], [class*="side-bar"]'))) {
        const r = el.getBoundingClientRect();
        const text = (el.innerText || '').trim();
        if (visible(el) && r.left < viewportWidth * 0.35 && r.width <= 420 && r.height > viewportHeight * 0.45) hide(el);
        else if (/开启新对话|deepseek/i.test(text) && r.left < viewportWidth * 0.35 && r.width <= 460) hide(el);
      }
      for (const input of Array.from(document.querySelectorAll('textarea,[contenteditable="true"],[role="textbox"],.ProseMirror'))) {
        if (!visible(input)) continue;
        let current = input;
        let best = input;
        let depth = 0;
        while (current && current !== document.body && depth < 8) {
          const r = current.getBoundingClientRect();
          const s = getComputedStyle(current);
          if (r.width >= Math.min(520, viewportWidth * 0.45) && r.bottom > viewportHeight - 280 && r.height <= 280) best = current;
          if ((s.position === 'fixed' || s.position === 'sticky') && r.bottom > viewportHeight - 320) best = current;
          current = current.parentElement;
          depth += 1;
        }
        hide(best);
      }
      for (const el of Array.from(document.body.querySelectorAll('*'))) {
        if (!visible(el)) continue;
        const r = el.getBoundingClientRect();
        const s = getComputedStyle(el);
        if ((s.position === 'fixed' || s.position === 'sticky') && r.bottom > viewportHeight - 220 && r.height <= 260) hide(el);
      }
      const style = document.createElement('style');
      style.setAttribute('data-geo-qa-capture-style', '1');
      style.textContent = `
        [data-geo-qa-capture-hidden="1"] { display: none !important; visibility: hidden !important; }
        [data-geo-qa-capture="1"] *, [data-geo-scroll-capture="1"] * { scroll-behavior: auto !important; }
        .ds-scroll-area__gutters, [class*="scrollbar"], [class*="floating"], [class*="Float"], [class*="composer"], [class*="Composer"] { visibility: hidden !important; }
      `;
      document.head.appendChild(style);
      document.documentElement.setAttribute('data-geo-qa-capture', '1');
      return true;
    })()
    """
    _cdp_evaluate(cdp, expression)


def _cdp_exit_qa_capture_mode(cdp: _CdpWebSocket) -> None:
    expression = r"""
    (() => {
      document.querySelector('[data-geo-qa-capture-style="1"]')?.remove();
      document.querySelectorAll('[data-geo-qa-capture-hidden="1"]').forEach(el => el.removeAttribute('data-geo-qa-capture-hidden'));
      document.documentElement.removeAttribute('data-geo-qa-capture');
      const previous = document.querySelector('[data-geo-scroll-capture="1"]');
      if (previous) previous.removeAttribute('data-geo-scroll-capture');
    })()
    """
    _cdp_evaluate(cdp, expression)


def _cdp_mark_scroll_container(cdp: _CdpWebSocket) -> dict | None:
    expression = r"""
    (() => {
      const previous = document.querySelector('[data-geo-scroll-capture="1"]');
      if (previous) previous.removeAttribute('data-geo-scroll-capture');
      const visible = el => {
        const r = el.getBoundingClientRect();
        const s = getComputedStyle(el);
        return r.width > 300 && r.height > 220 && s.visibility !== 'hidden' && s.display !== 'none';
      };
      const answerSelectors = ['.ds-markdown','[class*="markdown"]','article','[class*="answer"]','[class*="message-content"]'];
      let answer = null;
      for (const selector of answerSelectors) {
        const matches = Array.from(document.querySelectorAll(selector)).filter(el => visible(el) && (el.innerText || '').trim().length > 20);
        if (matches.length) { answer = matches[matches.length - 1]; break; }
      }
      if (!answer) return JSON.stringify({ ok: false, reason: 'answer_not_found' });
      const candidates = [];
      let current = answer.parentElement;
      while (current && current !== document.body) { candidates.push(current); current = current.parentElement; }
      candidates.push(document.scrollingElement || document.documentElement);
      const viewportWidth = window.innerWidth || document.documentElement.clientWidth;
      const viewportHeight = window.innerHeight || document.documentElement.clientHeight;
      const scored = candidates
        .filter(el => {
          const r = el.getBoundingClientRect();
          return visible(el) && el.scrollHeight > el.clientHeight + 120 && r.width >= Math.min(760, viewportWidth * 0.55) && r.height >= Math.min(420, viewportHeight * 0.45);
        })
        .map(el => {
          const r = el.getBoundingClientRect();
          const style = getComputedStyle(el);
          const overflowScore = /auto|scroll|overlay/i.test(style.overflowY || '') ? 100000 : 0;
          return { el, score: overflowScore + Math.min(el.scrollHeight, 20000) + Math.min(r.height, 2000) };
        })
        .sort((a, b) => b.score - a.score);
      const chosen = scored[0]?.el || document.scrollingElement || document.documentElement;
      const answerRect = answer.getBoundingClientRect();
      const rect = chosen.getBoundingClientRect();
      const textRects = [];
      for (const el of [answer, ...Array.from(answer.querySelectorAll('*'))]) {
        const r = el.getBoundingClientRect();
        const text = (el.innerText || el.textContent || '').trim();
        if (text.length > 0 && r.width > 80 && r.height > 8 && r.left >= 0 && r.right <= viewportWidth && r.width < viewportWidth * 0.92) textRects.push(r);
      }
      const lefts = textRects.map(r => r.left).filter(x => x > viewportWidth * 0.12).sort((a, b) => a - b);
      const rights = textRects.map(r => r.right).filter(x => x > viewportWidth * 0.35).sort((a, b) => a - b);
      const left = lefts.length ? lefts[0] : Math.max(0, answerRect.left);
      const right = rights.length ? rights[rights.length - 1] : Math.min(viewportWidth, answerRect.right);
      const paddingX = 72;
      const clipX = Math.max(0, Math.floor(left - paddingX));
      const clipY = Math.max(0, rect.top);
      const clipWidth = Math.max(760, Math.min(viewportWidth - clipX, Math.ceil(right - left + paddingX * 2)));
      const availableHeight = viewportHeight - clipY;
      const clipHeight = Math.max(320, Math.min(rect.height, availableHeight));
      chosen.setAttribute('data-geo-scroll-capture', '1');
      return JSON.stringify({
        ok: true,
        x: clipX,
        y: clipY,
        width: clipWidth,
        height: clipHeight,
        scrollHeight: chosen.scrollHeight,
        clientHeight: chosen.clientHeight,
        maxScroll: Math.max(0, chosen.scrollHeight - chosen.clientHeight),
        initialScrollTop: chosen.scrollTop
      });
    })()
    """
    info = json.loads(_cdp_evaluate(cdp, expression) or "{}")
    return info if info.get("ok") else None


def _cdp_set_marked_scroll_top(cdp: _CdpWebSocket, scroll_top: int) -> None:
    expression = f"""
    (() => {{
      const target = document.querySelector('[data-geo-scroll-capture="1"]') || document.scrollingElement || document.documentElement;
      target.scrollTop = {int(scroll_top)};
    }})()
    """
    _cdp_evaluate(cdp, expression)


def _image_difference_score(image_a, image_b) -> float:
    from PIL import ImageChops, ImageStat

    width = min(360, image_a.width, image_b.width)
    height = min(160, image_a.height, image_b.height)
    if width <= 0 or height <= 0:
        return float("inf")
    sample_a = image_a.resize((width, height)).convert("L")
    sample_b = image_b.resize((width, height)).convert("L")
    diff = ImageChops.difference(sample_a, sample_b)
    return float(ImageStat.Stat(diff).mean[0])


def _best_vertical_overlap(previous_image, current_image, expected_overlap: int) -> int:
    probe_height = min(260, current_image.height // 3, previous_image.height // 3)
    if probe_height >= 80:
        probe = current_image.crop((0, 0, current_image.width, probe_height))
        best_y = None
        best_score = float("inf")
        step_y = max(8, previous_image.height // 80)
        for y in range(0, max(1, previous_image.height - probe_height + 1), step_y):
            candidate = previous_image.crop((0, y, previous_image.width, y + probe_height))
            score = _image_difference_score(candidate, probe)
            if score < best_score:
                best_score = score
                best_y = y
        if best_y is not None and best_score < 5:
            return max(0, previous_image.height - best_y)

    max_overlap = min(previous_image.height, current_image.height, max(expected_overlap * 4, expected_overlap + 500, int(current_image.height * 0.75)))
    min_overlap = min(max_overlap, max(24, expected_overlap // 3))
    if max_overlap <= min_overlap:
        return min(previous_image.height, current_image.height, max(0, expected_overlap))
    step = max(6, (max_overlap - min_overlap) // 32)
    best_overlap = min(max_overlap, max(min_overlap, expected_overlap))
    best_score = float("inf")
    for overlap in range(min_overlap, max_overlap + 1, step):
        previous_strip = previous_image.crop((0, previous_image.height - overlap, previous_image.width, previous_image.height))
        current_strip = current_image.crop((0, 0, current_image.width, overlap))
        score = _image_difference_score(previous_strip, current_strip)
        if score < best_score:
            best_score = score
            best_overlap = overlap
    return best_overlap


def _cdp_capture_scroll_stitched_evidence(screenshot_path: Path) -> tuple[str, int]:
    from io import BytesIO
    from PIL import Image

    ws_url = _cdp_deepseek_ws_url()
    if not ws_url:
        raise RuntimeError("cdp_not_available")
    screenshot_path.parent.mkdir(parents=True, exist_ok=True)
    chunks = []
    clip = {"height": 1}
    capture_height = 1
    max_scroll = 0
    with _CdpWebSocket(ws_url) as cdp:
        cdp.send_cmd("Page.enable")
        cdp.send_cmd("Runtime.enable")
        _cdp_bring_to_front(cdp)
        target = None
        try:
            _cdp_enter_qa_capture_mode(cdp)
            time.sleep(0.2)
            target = _cdp_mark_scroll_container(cdp)
            if not target:
                raise RuntimeError("scroll_container_not_found")
            clip = {
                "x": int(target["x"]),
                "y": int(target["y"]),
                "width": max(1, int(target["width"])),
                "height": max(1, int(target["height"])),
                "scale": 1,
            }
            max_scroll = max(0, int(target["maxScroll"]))
            capture_height = max(1, int(clip["height"]))
            overlap_css = min(280, max(120, capture_height // 5))
            step = max(1, capture_height - overlap_css)
            positions = list(range(0, max_scroll + 1, step))
            if not positions or positions[-1] != max_scroll:
                positions.append(max_scroll)
            positions = positions[:DESKTOP_CAPTURE_MAX_PAGES]
            for scroll_top in positions:
                _cdp_set_marked_scroll_top(cdp, scroll_top)
                time.sleep(0.45)
                data = cdp.send_cmd("Page.captureScreenshot", {"format": "png", "fromSurface": False, "captureBeyondViewport": False, "clip": clip})
                image = Image.open(BytesIO(base64.b64decode(data["data"]))).convert("RGB")
                chunks.append((scroll_top, image))
        finally:
            if target:
                try:
                    _cdp_set_marked_scroll_top(cdp, int(target.get("initialScrollTop") or max_scroll))
                except Exception:
                    pass
            try:
                _cdp_exit_qa_capture_mode(cdp)
            except Exception:
                pass

    if not chunks:
        raise RuntimeError("scroll_capture_no_chunks")
    scale = chunks[0][1].height / max(1, clip["height"])
    expected_overlap_px = int(min(280, max(120, capture_height // 5)) * scale)
    width = chunks[0][1].width
    parts = []
    for index, (_scroll_top, image) in enumerate(chunks):
        if index == 0:
            parts.append(image)
            continue
        overlap = _best_vertical_overlap(parts[-1], image, expected_overlap_px)
        crop_top = min(image.height - 1, max(0, overlap))
        parts.append(image.crop((0, crop_top, image.width, image.height)))
    total_height = sum(part.height for part in parts)
    stitched = Image.new("RGB", (width, max(1, total_height)), "white")
    y = 0
    for part in parts:
        stitched.paste(part, (0, y))
        y += part.height
    stitched.save(screenshot_path)
    _add_timestamp_to_image(screenshot_path)
    return str(screenshot_path), len(chunks)


def _cdp_capture_deepseek_evidence(screenshot_path: Path, question: str) -> tuple[str, str, str]:
    ws_url = _cdp_deepseek_ws_url()
    if not ws_url:
        raise RuntimeError("cdp_not_available")

    create_clean_capture = r"""
    (function (questionText) {
      const previous = document.querySelector('[data-geo-clean-capture="1"]');
      if (previous) previous.remove();
      const normalizeText = text => (text || '').replace(/\s+/g, '').trim();
      const questionNeedle = normalizeText(questionText).slice(0, 32);
      const pageText = normalizeText(document.body ? document.body.innerText : '');
      if (questionNeedle.length >= 8 && !pageText.includes(questionNeedle)) return JSON.stringify({ ok: false, reason: 'question_not_found_on_page' });
      const visible = el => {
        const r = el.getBoundingClientRect();
        const s = getComputedStyle(el);
        return r.width > 240 && r.height > 20 && s.visibility !== 'hidden' && s.display !== 'none';
      };
      const answerSelectors = ['.ds-markdown','[class*="markdown"]','article','[class*="answer"]','[class*="message"]'];
      const messageSelectors = ['.ds-message','[class*="message"]'];
      const normalizeClone = node => {
        const nodes = [node, ...Array.from(node.querySelectorAll('*'))];
        for (const item of nodes) {
          const set = (name, value) => item.style.setProperty(name, value, 'important');
          set('max-height', 'none'); set('overflow', 'visible'); set('overflow-x', 'visible'); set('overflow-y', 'visible');
          set('-webkit-line-clamp', 'unset'); set('line-clamp', 'unset'); set('contain', 'none'); set('content-visibility', 'visible');
          set('transform', 'none'); set('clip-path', 'none'); set('mask-image', 'none'); set('-webkit-mask-image', 'none');
          const position = getComputedStyle(item).position;
          if (position === 'fixed' || position === 'sticky') set('position', 'static');
          if (!['IMG', 'SVG', 'CANVAS', 'VIDEO'].includes(item.tagName)) set('height', 'auto');
        }
        return node;
      };
      let answer = null;
      for (const selector of answerSelectors) {
        const matches = Array.from(document.querySelectorAll(selector)).filter(el => visible(el) && (el.innerText || '').trim().length > 20);
        if (matches.length) { answer = matches[matches.length - 1]; break; }
      }
      if (!answer) return JSON.stringify({ ok: false, reason: 'answer_not_found' });
      let answerMessage = answer;
      for (const selector of messageSelectors) {
        const match = answer.closest(selector);
        if (match) { answerMessage = match; break; }
      }
      const scope = answerMessage.closest('.ds-virtual-list-visible-items,.ds-virtual-list-items,main') || answerMessage.parentElement || document.body;
      const messages = Array.from(new Set(messageSelectors.flatMap(selector => Array.from(scope.querySelectorAll(selector))))).filter(el => visible(el) && (el.innerText || '').trim());
      const answerIndex = messages.findIndex(el => el === answerMessage || el.contains(answer));
      let questionMessage = null;
      for (let index = answerIndex - 1; index >= 0; index -= 1) {
        const candidate = messages[index];
        if (!candidate.contains(answer)) { questionMessage = candidate; break; }
      }
      let answerBlock = answerMessage;
      let current = answerMessage.parentElement;
      let depth = 0;
      while (current && current !== document.body && depth < 5) {
        const rect = current.getBoundingClientRect();
        const text = (current.innerText || '').trim();
        const messageCount = current.querySelectorAll('.ds-message').length;
        if (messageCount <= 1 && rect.width >= answerMessage.getBoundingClientRect().width - 30 && rect.width <= answerMessage.getBoundingClientRect().width + 140 && text.includes((answer.innerText || '').trim().slice(0, 80))) answerBlock = current;
        current = current.parentElement;
        depth += 1;
      }
      const sourceBlocks = [];
      if (questionMessage) sourceBlocks.push(questionMessage);
      sourceBlocks.push(answerBlock);
      const answerText = (answerBlock.innerText || answerMessage.innerText || answer.innerText || '').trim();
      const widestContent = Math.max(answer.getBoundingClientRect().width || 0, ...sourceBlocks.flatMap(block => [block, ...Array.from(block.querySelectorAll('*'))].map(el => el.scrollWidth || 0)));
      const contentWidth = Math.max(760, Math.min(1600, Math.ceil(widestContent || 980)));
      const captureWidth = Math.max(window.innerWidth || 1200, contentWidth + 64);
      const root = document.createElement('div');
      root.setAttribute('data-geo-clean-capture', '1');
      root.style.position = 'absolute'; root.style.left = '0'; root.style.top = '0'; root.style.zIndex = '2147483647';
      root.style.width = `${captureWidth}px`; root.style.minHeight = '100vh'; root.style.boxSizing = 'border-box';
      root.style.padding = '32px 0 48px'; root.style.background = '#fff'; root.style.color = '#111';
      const style = document.createElement('style');
      style.textContent = `
        [data-geo-clean-capture="1"], [data-geo-clean-capture="1"] * {
          scroll-behavior: auto !important; max-height: none !important; overflow: visible !important;
          -webkit-line-clamp: unset !important; line-clamp: unset !important; contain: none !important;
        }
        [data-geo-clean-capture="1"] textarea, [data-geo-clean-capture="1"] [contenteditable="true"], [data-geo-clean-capture="1"] input[type="file"] { display: none !important; }
        [data-geo-clean-capture="1"] [style*="position: sticky"], [data-geo-clean-capture="1"] [style*="position: fixed"] { position: static !important; }
        [data-geo-fallback-question="1"] {
          align-self: flex-end; max-width: 70%; box-sizing: border-box; padding: 12px 16px; border-radius: 18px;
          background: #eef4ff; color: #111827; font: 15px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
          white-space: pre-wrap; overflow-wrap: anywhere;
        }
      `;
      root.appendChild(style);
      const content = document.createElement('div');
      content.style.width = `${contentWidth}px`; content.style.maxWidth = `${captureWidth - 64}px`; content.style.margin = '0 auto';
      content.style.display = 'flex'; content.style.flexDirection = 'column'; content.style.gap = '24px';
      if (questionMessage) content.appendChild(normalizeClone(questionMessage.cloneNode(true)));
      else {
        const fallbackQuestion = document.createElement('div');
        fallbackQuestion.setAttribute('data-geo-fallback-question', '1');
        fallbackQuestion.textContent = questionText || '';
        content.appendChild(fallbackQuestion);
      }
      content.appendChild(normalizeClone(answerBlock.cloneNode(true)));
      root.appendChild(content);
      document.body.appendChild(root);
      const height = Math.ceil(root.scrollHeight);
      root.style.minHeight = `${height}px`;
      document.documentElement.dataset.geoPreviousMinHeight = document.documentElement.style.minHeight || '';
      document.body.dataset.geoPreviousMinHeight = document.body.style.minHeight || '';
      document.documentElement.style.minHeight = `${height}px`;
      document.body.style.minHeight = `${height}px`;
      window.scrollTo(0, 0);
      return JSON.stringify({ ok: true, width: captureWidth, height, answerText });
    })
    """
    cleanup = """
    (function () {
      const capture = document.querySelector('[data-geo-clean-capture="1"]');
      if (capture) capture.remove();
      if (document.documentElement.dataset.geoPreviousMinHeight !== undefined) {
        document.documentElement.style.minHeight = document.documentElement.dataset.geoPreviousMinHeight || '';
        delete document.documentElement.dataset.geoPreviousMinHeight;
      }
      if (document.body.dataset.geoPreviousMinHeight !== undefined) {
        document.body.style.minHeight = document.body.dataset.geoPreviousMinHeight || '';
        delete document.body.dataset.geoPreviousMinHeight;
      }
    })()
    """
    screenshot_path.parent.mkdir(parents=True, exist_ok=True)
    with _CdpWebSocket(ws_url) as cdp:
        cdp.send_cmd("Page.enable")
        cdp.send_cmd("Runtime.enable")
        expression = f"({create_clean_capture})({json.dumps(question)})"
        result = cdp.send_cmd("Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True})
        info = json.loads(((result.get("result") or {}).get("value")) or "{}")
        if not info.get("ok"):
            raise RuntimeError(f"cdp_clean_capture_failed: {info.get('reason')}")
        width = max(1, int(info.get("width") or 1400))
        height = max(1, int(info.get("height") or 1000))
        answer_text = str(info.get("answerText") or "").strip()
        estimated_text_height = max(900, int((len(answer_text) / 42) * 34) + 420)
        clean_capture_too_short = height < min(2800, int(estimated_text_height * 0.75))
        if len(answer_text) > 700 or clean_capture_too_short:
            try:
                cdp.send_cmd("Runtime.evaluate", {"expression": cleanup, "returnByValue": True})
            except Exception:
                pass
            screenshot, pages = _cdp_capture_scroll_stitched_evidence(screenshot_path)
            return screenshot, answer_text, f"CDP真实页面滚动拼接_pages={pages}"
        data = cdp.send_cmd(
            "Page.captureScreenshot",
            {
                "format": "png",
                "fromSurface": False,
                "captureBeyondViewport": True,
                "clip": {"x": 0, "y": 0, "width": width, "height": height, "scale": 1},
            },
        )
        screenshot_path.write_bytes(base64.b64decode(data["data"]))
        _add_timestamp_to_image(screenshot_path)
        try:
            cdp.send_cmd("Runtime.evaluate", {"expression": cleanup, "returnByValue": True})
        except Exception:
            pass
        return str(screenshot_path), answer_text, "CDP真实页面全图"


def _desktop_screenshot(screenshot_path: Path) -> str:
    screenshot_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(["screencapture", "-x", str(screenshot_path)], check=True)
        _add_timestamp_to_image(screenshot_path)
        return str(screenshot_path)
    except Exception:
        return ""


def _add_timestamp_to_image(image_path: Path, captured_at: str | None = None) -> None:
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return
    captured_at = captured_at or _now()
    label = f"采集时间：{captured_at}"
    try:
        image = Image.open(image_path).convert("RGBA")
        draw = ImageDraw.Draw(image)
        font = _load_cjk_font(max(18, min(28, image.width // 70)))
        padding_x = 18
        padding_y = 10
        margin = 24
        bbox = draw.textbbox((0, 0), label, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
        box_width = text_width + padding_x * 2
        box_height = text_height + padding_y * 2
        x1 = max(margin, image.width - box_width - margin)
        y1 = margin
        x2 = x1 + box_width
        y2 = y1 + box_height
        draw.rounded_rectangle((x1, y1, x2, y2), radius=10, fill=(255, 255, 255, 225), outline=(209, 213, 219, 230), width=1)
        draw.text((x1 + padding_x, y1 + padding_y), label, fill=(17, 24, 39, 255), font=font)
        image.convert("RGB").save(image_path)
    except Exception:
        pass


def _load_cjk_font(size: int):
    from PIL import ImageFont

    font_paths = (
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/Library/Fonts/Arial Unicode.ttf",
    )
    for font_path in font_paths:
        try:
            return ImageFont.truetype(font_path, size)
        except Exception:
            continue
    return ImageFont.load_default()
