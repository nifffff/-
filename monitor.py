#!/usr/bin/env python3
"""Background, HTTP-only monitor for Cocofa account data."""

from __future__ import annotations

import argparse
import base64
import io
import json
import logging
import os
import re
import signal
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence
from urllib.parse import urljoin, urlparse


LOGGER = logging.getLogger("cocofa-monitor")
MONITORED_FIELDS = ("status", "expires_at", "coins", "balance")
MIN_POLL_INTERVAL_SECONDS = 5


class AuthenticationExpired(RuntimeError):
    pass


@dataclass(frozen=True)
class Account:
    key: str
    provider_id: str
    remark: str
    nickname: str
    status: str
    expires_at: str
    coins: str
    balance: str

    @classmethod
    def from_api(cls, raw: Mapping[str, Any], fallback_index: int = 0) -> "Account":
        provider_id = clean_text(raw.get("id") or raw.get("accountId") or raw.get("provider_id"))
        remark = clean_text(raw.get("remark"))
        nickname = clean_text(raw.get("nickname"))
        base_key = provider_id or remark or nickname or "unknown"
        key = base_key if fallback_index == 0 else "%s#%d" % (base_key, fallback_index + 1)
        status_value = raw.get("status")
        if status_value == 1 or status_value == "1":
            status = "运行中"
        elif status_value == 0 or status_value == "0":
            status = "已停止"
        else:
            status = clean_text(status_value)
        return cls(
            key=key,
            provider_id=provider_id,
            remark=remark,
            nickname=nickname,
            status=status,
            expires_at=clean_text(raw.get("expireTime") or raw.get("expires_at")),
            coins=normalize_number(raw.get("coinBalance") if "coinBalance" in raw else raw.get("coins")),
            balance=normalize_money(raw.get("cashBalance") if "cashBalance" in raw else raw.get("balance")),
        )


def clean_text(value: Any) -> str:
    return " ".join(str(value if value is not None else "").replace("\u00a0", " ").split())


def normalize_number(value: Any) -> str:
    return clean_text(value).replace(",", "")


def normalize_money(value: Any) -> str:
    text = normalize_number(value)
    return text[:-1].strip() if text.endswith("元") else text


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def resolve_path(config_path: Path, value: str) -> Path:
    path = Path(os.path.expanduser(value))
    if not path.is_absolute():
        path = config_path.parent / path
    return path.resolve()


def assert_same_origin(target_url: str, endpoint_url: str) -> None:
    target = urlparse(target_url)
    endpoint = urlparse(endpoint_url)
    if (target.scheme, target.hostname, target.port) != (endpoint.scheme, endpoint.hostname, endpoint.port):
        raise ValueError("拒绝跨站接口：%s" % endpoint_url)


def load_config(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        config = json.load(handle)
    required = {
        "target_url",
        "captcha_endpoint",
        "login_endpoint",
        "accounts_endpoint",
        "poll_interval_seconds",
        "snapshot_file",
        "change_log_file",
    }
    missing = sorted(required.difference(config))
    if missing:
        raise ValueError("配置缺少字段：%s" % ", ".join(missing))
    if int(config["poll_interval_seconds"]) < MIN_POLL_INTERVAL_SECONDS:
        raise ValueError("poll_interval_seconds 不能小于 %d 秒" % MIN_POLL_INTERVAL_SECONDS)
    if not str(config["target_url"]).startswith(("http://", "https://")):
        raise ValueError("target_url 必须是 http:// 或 https:// 地址")
    for key in ("captcha_endpoint", "login_endpoint", "accounts_endpoint"):
        assert_same_origin(str(config["target_url"]), urljoin(str(config["target_url"]), str(config[key])))
    return config


def make_accounts(raw_accounts: Iterable[Mapping[str, Any]]) -> Dict[str, Account]:
    accounts: Dict[str, Account] = {}
    counts: MutableMapping[str, int] = {}
    for raw in raw_accounts:
        provider_id = clean_text(raw.get("id") or raw.get("accountId") or raw.get("provider_id"))
        remark = clean_text(raw.get("remark"))
        nickname = clean_text(raw.get("nickname"))
        base_key = provider_id or remark or nickname or "unknown"
        occurrence = counts.get(base_key, 0)
        counts[base_key] = occurrence + 1
        account = Account.from_api(raw, fallback_index=occurrence)
        accounts[account.key] = account
    duplicates = sorted(key for key, count in counts.items() if count > 1)
    if duplicates:
        LOGGER.warning("发现重复账号标识，已按接口顺序添加序号：%s", ", ".join(duplicates))
    return accounts


def diff_snapshots(
    previous: Mapping[str, Mapping[str, Any]], current: Mapping[str, Mapping[str, Any]]
) -> List[Dict[str, Any]]:
    changes: List[Dict[str, Any]] = []
    previous_keys = set(previous)
    current_keys = set(current)
    for key in sorted(current_keys - previous_keys):
        changes.append({"type": "account_added", "account_key": key, "account": dict(current[key])})
    for key in sorted(previous_keys - current_keys):
        changes.append({"type": "account_removed", "account_key": key, "account": dict(previous[key])})
    for key in sorted(previous_keys & current_keys):
        before = previous[key]
        after = current[key]
        for field in MONITORED_FIELDS:
            if clean_text(before.get(field)) != clean_text(after.get(field)):
                changes.append(
                    {
                        "type": "field_changed",
                        "account_key": key,
                        "field": field,
                        "old": before.get(field, ""),
                        "new": after.get(field, ""),
                        "account": dict(after),
                    }
                )
    return changes


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(str(temporary), str(path))


def load_snapshot(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict) or not isinstance(payload.get("accounts"), dict):
        raise ValueError("快照文件格式无效：%s" % path)
    return payload


def append_changes(path: Path, changes: Sequence[Mapping[str, Any]], observed_at: str) -> None:
    if not changes:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for change in changes:
            record = {"observed_at": observed_at, **dict(change)}
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()


def human_change(change: Mapping[str, Any]) -> str:
    labels = {"status": "运行状态", "expires_at": "到期时间", "coins": "金币", "balance": "余额"}
    account = change.get("account") or {}
    name = account.get("remark") or account.get("nickname") or change.get("account_key")
    if change["type"] == "account_added":
        return "账号新增 [%s] 状态=%s 到期=%s 金币=%s 余额=%s" % (
            name,
            account.get("status", ""),
            account.get("expires_at", ""),
            account.get("coins", ""),
            account.get("balance", ""),
        )
    if change["type"] == "account_removed":
        return "账号删除 [%s]" % name
    return "%s [%s] %s -> %s" % (
        labels.get(str(change.get("field")), str(change.get("field"))),
        name,
        change.get("old", ""),
        change.get("new", ""),
    )


class CocofaHttpClient:
    """Narrow client: one explicit login POST; all monitoring calls are GET."""

    def __init__(self, config: Mapping[str, Any]) -> None:
        try:
            import requests
        except ImportError as exc:
            raise RuntimeError("缺少 requests，请先执行 pip install -r requirements.txt") from exc
        self.config = config
        self.base_url = str(config["target_url"])
        self.timeout = float(config.get("request_timeout_seconds", 15))
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json", "User-Agent": "CocofaReadOnlyMonitor/1.0"})

    def _url(self, endpoint_key: str) -> str:
        url = urljoin(self.base_url, str(self.config[endpoint_key]))
        assert_same_origin(self.base_url, url)
        return url

    @staticmethod
    def _json(response: Any, purpose: str) -> Mapping[str, Any]:
        try:
            payload = response.json()
        except Exception as exc:
            raise RuntimeError("%s返回的不是 JSON（HTTP %s）" % (purpose, response.status_code)) from exc
        if not isinstance(payload, dict):
            raise RuntimeError("%s返回格式无效" % purpose)
        return payload

    def fetch_captcha(self) -> Dict[str, str]:
        response = self.session.get(self._url("captcha_endpoint"), timeout=self.timeout, allow_redirects=False)
        response.raise_for_status()
        payload = self._json(response, "验证码接口")
        data = payload.get("data") or {}
        if not payload.get("success") or not isinstance(data, dict) or not data.get("id") or not data.get("image"):
            raise RuntimeError(clean_text(payload.get("message")) or "验证码接口缺少 id/image")
        return {"id": str(data["id"]), "image": str(data["image"])}

    def login(self, username: str, password: str, captcha: str, captcha_id: str) -> None:
        # This is the only non-GET request in the program. It is fixed to the
        # same-origin login endpoint and is never reused for account management.
        payload = {"username": username, "password": password, "captcha": captcha, "captchaId": captcha_id}
        response = self.session.post(
            self._url("login_endpoint"),
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=self.timeout,
            allow_redirects=False,
        )
        payload.clear()
        response.raise_for_status()
        result = self._json(response, "登录接口")
        if not result.get("success"):
            raise RuntimeError(clean_text(result.get("message")) or "登录失败")

    def fetch_accounts(self) -> Dict[str, Account]:
        response = self.session.get(self._url("accounts_endpoint"), timeout=self.timeout, allow_redirects=False)
        if response.status_code in (301, 302, 303, 307, 308, 401, 403):
            raise AuthenticationExpired("登录会话已失效，请重新启动监控并登录")
        response.raise_for_status()
        payload = self._json(response, "账号列表接口")
        if not payload.get("success"):
            message = clean_text(payload.get("message"))
            if "登录" in message or "认证" in message:
                raise AuthenticationExpired(message or "登录会话已失效")
            raise RuntimeError(message or "账号列表接口报告失败")
        raw_accounts = payload.get("accounts")
        if not isinstance(raw_accounts, list):
            raise RuntimeError("账号列表接口缺少 accounts 数组；请检查 accounts_endpoint 配置")
        return make_accounts(item for item in raw_accounts if isinstance(item, dict))

    def close(self) -> None:
        self.session.cookies.clear()
        self.session.close()


def captcha_to_png(image_data: str) -> bytes:
    if not image_data.startswith("data:") or "," not in image_data:
        raise RuntimeError("验证码不是受支持的 data URI")
    header, encoded = image_data.split(",", 1)
    raw = base64.b64decode(encoded) if ";base64" in header else encoded.encode("utf-8")
    if "image/svg+xml" in header:
        return render_captcha_svg(raw, output_size=(240, 80))
    return raw


SVG_TOKEN_RE = re.compile(r"[A-Za-z]|[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?")


def _svg_subpaths(path_data: str) -> List[List[tuple]]:
    """Flatten the simple SVG paths used by Cocofa captchas into polygons."""
    tokens = SVG_TOKEN_RE.findall(path_data)
    counts = {"M": 2, "L": 2, "H": 1, "V": 1, "C": 6, "S": 4, "Q": 4, "T": 2}
    paths: List[List[tuple]] = []
    current_path: List[tuple] = []
    current = (0.0, 0.0)
    start = current
    previous_control: Optional[tuple] = None
    command = ""
    index = 0

    def point(x: float, y: float, relative: bool) -> tuple:
        return (x + current[0], y + current[1]) if relative else (x, y)

    while index < len(tokens):
        token = tokens[index]
        if token.isalpha():
            command = token
            index += 1
        if not command:
            raise RuntimeError("验证码 SVG path 缺少命令")
        upper = command.upper()
        relative = command.islower()
        if upper == "Z":
            if current_path:
                current_path.append(start)
                paths.append(current_path)
                current_path = []
            current = start
            previous_control = None
            command = ""
            continue
        required = counts.get(upper)
        if required is None or index + required > len(tokens):
            raise RuntimeError("验证码 SVG 包含不支持的 path 命令：%s" % command)
        values = [float(value) for value in tokens[index : index + required]]
        index += required
        if upper == "M":
            target = point(values[0], values[1], relative)
            if current_path:
                paths.append(current_path)
            current_path = [target]
            current = target
            start = target
            command = "l" if relative else "L"
            previous_control = None
        elif upper == "L":
            current = point(values[0], values[1], relative)
            current_path.append(current)
            previous_control = None
        elif upper == "H":
            current = ((current[0] + values[0]) if relative else values[0], current[1])
            current_path.append(current)
            previous_control = None
        elif upper == "V":
            current = (current[0], (current[1] + values[0]) if relative else values[0])
            current_path.append(current)
            previous_control = None
        elif upper == "Q":
            control = point(values[0], values[1], relative)
            target = point(values[2], values[3], relative)
            origin = current
            for step in range(1, 13):
                t = step / 12.0
                current_path.append(
                    (
                        (1 - t) ** 2 * origin[0] + 2 * (1 - t) * t * control[0] + t**2 * target[0],
                        (1 - t) ** 2 * origin[1] + 2 * (1 - t) * t * control[1] + t**2 * target[1],
                    )
                )
            current = target
            previous_control = control
        elif upper == "T":
            control = (
                2 * current[0] - previous_control[0],
                2 * current[1] - previous_control[1],
            ) if previous_control else current
            target = point(values[0], values[1], relative)
            origin = current
            for step in range(1, 13):
                t = step / 12.0
                current_path.append(
                    (
                        (1 - t) ** 2 * origin[0] + 2 * (1 - t) * t * control[0] + t**2 * target[0],
                        (1 - t) ** 2 * origin[1] + 2 * (1 - t) * t * control[1] + t**2 * target[1],
                    )
                )
            current = target
            previous_control = control
        elif upper in ("C", "S"):
            if upper == "C":
                control1 = point(values[0], values[1], relative)
                control2 = point(values[2], values[3], relative)
                target = point(values[4], values[5], relative)
            else:
                control1 = (
                    2 * current[0] - previous_control[0],
                    2 * current[1] - previous_control[1],
                ) if previous_control else current
                control2 = point(values[0], values[1], relative)
                target = point(values[2], values[3], relative)
            origin = current
            for step in range(1, 13):
                t = step / 12.0
                current_path.append(
                    (
                        (1 - t) ** 3 * origin[0] + 3 * (1 - t) ** 2 * t * control1[0] + 3 * (1 - t) * t**2 * control2[0] + t**3 * target[0],
                        (1 - t) ** 3 * origin[1] + 3 * (1 - t) ** 2 * t * control1[1] + 3 * (1 - t) * t**2 * control2[1] + t**3 * target[1],
                    )
                )
            current = target
            previous_control = control2
    if current_path:
        paths.append(current_path)
    return paths


def render_captcha_svg(svg_bytes: bytes, output_size: tuple) -> bytes:
    """Render Cocofa's compact SVG captcha with Pillow only (Windows/macOS safe)."""
    try:
        from PIL import Image, ImageColor, ImageDraw
    except ImportError as exc:
        raise RuntimeError("SVG 验证码需要 Pillow，请执行 pip install -r requirements.txt") from exc
    root = ET.fromstring(svg_bytes)
    view_box = [float(value) for value in root.attrib.get("viewBox", "0 0 120 40").replace(",", " ").split()]
    if len(view_box) != 4 or view_box[2] <= 0 or view_box[3] <= 0:
        raise RuntimeError("验证码 SVG viewBox 无效")
    x0, y0, width, height = view_box
    sx, sy = output_size[0] / width, output_size[1] / height
    background = "#ffffff"
    for element in root:
        if element.tag.rsplit("}", 1)[-1] == "rect" and element.attrib.get("fill", "none") != "none":
            background = element.attrib.get("fill", background)
            break
    image = Image.new("RGB", output_size, ImageColor.getrgb(background))
    draw = ImageDraw.Draw(image)

    def transform(points: List[tuple]) -> List[tuple]:
        return [((point[0] - x0) * sx, (point[1] - y0) * sy) for point in points]

    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] != "path" or not element.attrib.get("d"):
            continue
        styles = {}
        for item in element.attrib.get("style", "").split(";"):
            if ":" in item:
                key, value = item.split(":", 1)
                styles[key.strip()] = value.strip()
        fill = element.attrib.get("fill", styles.get("fill", "black"))
        stroke = element.attrib.get("stroke", styles.get("stroke", "none"))
        subpaths = [transform(path) for path in _svg_subpaths(element.attrib["d"])]
        if fill != "none":
            fill_color = ImageColor.getrgb(fill)
            # Draw larger contours first. Opposite winding contours are holes.
            def area(points: List[tuple]) -> float:
                return sum(
                    points[i][0] * points[(i + 1) % len(points)][1]
                    - points[(i + 1) % len(points)][0] * points[i][1]
                    for i in range(len(points))
                ) / 2.0

            ordered = sorted((path for path in subpaths if len(path) >= 3), key=lambda p: abs(area(p)), reverse=True)
            outer_sign = 1 if not ordered or area(ordered[0]) >= 0 else -1
            for path in ordered:
                color = fill_color if (1 if area(path) >= 0 else -1) == outer_sign else ImageColor.getrgb(background)
                draw.polygon(path, fill=color)
        if stroke != "none":
            stroke_color = ImageColor.getrgb(stroke)
            stroke_width = max(1, int(float(element.attrib.get("stroke-width", styles.get("stroke-width", "1"))) * max(sx, sy)))
            for path in subpaths:
                if len(path) >= 2:
                    draw.line(path, fill=stroke_color, width=stroke_width, joint="curve")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def authenticate_with_local_gui(client: CocofaHttpClient, config: MutableMapping[str, Any]) -> bool:
    try:
        import tkinter as tk
        from PIL import Image, ImageTk
    except ImportError as exc:
        raise RuntimeError("缺少本地界面组件，请执行 pip install -r requirements.txt") from exc

    root = tk.Tk()
    root.title("Cocofa 后台监控登录")
    root.resizable(False, False)
    username_var = tk.StringVar()
    password_var = tk.StringVar()
    captcha_var = tk.StringVar()
    interval_var = tk.StringVar(value=str(config["poll_interval_seconds"]))
    status_var = tk.StringVar(value="凭据只用于本次登录，Cookie 仅保存在本进程内。")
    captcha_id = [""]
    captcha_photo: List[Any] = [None]
    authenticated = [False]

    tk.Label(root, text="Cocofa 后台监控", font=("TkDefaultFont", 16, "bold")).grid(
        row=0, column=0, columnspan=3, padx=18, pady=(18, 12)
    )
    tk.Label(root, text="用户名").grid(row=1, column=0, padx=(18, 8), pady=6, sticky="e")
    username_entry = tk.Entry(root, textvariable=username_var, width=30)
    username_entry.grid(row=1, column=1, columnspan=2, padx=(0, 18), pady=6, sticky="ew")
    tk.Label(root, text="密码").grid(row=2, column=0, padx=(18, 8), pady=6, sticky="e")
    tk.Entry(root, textvariable=password_var, show="*", width=30).grid(
        row=2, column=1, columnspan=2, padx=(0, 18), pady=6, sticky="ew"
    )
    tk.Label(root, text="验证码").grid(row=3, column=0, padx=(18, 8), pady=6, sticky="e")
    tk.Entry(root, textvariable=captcha_var, width=15).grid(row=3, column=1, padx=(0, 8), pady=6, sticky="ew")
    captcha_label = tk.Label(root, text="加载中…", cursor="hand2")
    captcha_label.grid(row=3, column=2, padx=(0, 18), pady=6)
    tk.Label(root, text="轮询秒数").grid(row=4, column=0, padx=(18, 8), pady=6, sticky="e")
    tk.Entry(root, textvariable=interval_var, width=15).grid(row=4, column=1, padx=(0, 8), pady=6, sticky="w")
    tk.Label(root, text="最少 5 秒").grid(row=4, column=2, padx=(0, 18), pady=6, sticky="w")
    tk.Label(root, textvariable=status_var, fg="#555555", wraplength=420, justify="left").grid(
        row=5, column=0, columnspan=3, padx=18, pady=(8, 4), sticky="w"
    )
    login_button = tk.Button(root, text="登录并开始后台监控", width=24)
    login_button.grid(row=6, column=0, columnspan=3, padx=18, pady=(8, 18))

    def refresh_captcha(_event: Any = None) -> None:
        try:
            status_var.set("正在刷新验证码…")
            root.update_idletasks()
            result = client.fetch_captcha()
            captcha_id[0] = result["id"]
            png = captcha_to_png(result["image"])
            image = Image.open(io.BytesIO(png))
            captcha_photo[0] = ImageTk.PhotoImage(image)
            captcha_label.configure(image=captcha_photo[0], text="")
            captcha_var.set("")
            status_var.set("凭据只用于本次登录。点击验证码图片可刷新。")
        except Exception as exc:
            status_var.set("验证码加载失败：%s" % clean_text(exc))

    def submit(_event: Any = None) -> None:
        username = username_var.get().strip()
        password = password_var.get()
        captcha = captcha_var.get().strip()
        try:
            interval = int(interval_var.get())
        except ValueError:
            interval = 0
        if not username or not password or not captcha:
            status_var.set("用户名、密码和验证码都必须填写。")
            return
        if interval < MIN_POLL_INTERVAL_SECONDS:
            status_var.set("轮询间隔不能小于 %d 秒。" % MIN_POLL_INTERVAL_SECONDS)
            return
        login_button.configure(state="disabled")
        status_var.set("正在登录…")
        root.update_idletasks()
        try:
            client.login(username, password, captcha, captcha_id[0])
            config["poll_interval_seconds"] = interval
            authenticated[0] = True
            status_var.set("登录成功，后台监控即将启动。")
            root.after(600, root.destroy)
        except Exception as exc:
            status_var.set(clean_text(exc))
            refresh_captcha()
        finally:
            password = ""
            password_var.set("")
            captcha_var.set("")
            login_button.configure(state="normal")

    captcha_label.bind("<Button-1>", refresh_captcha)
    login_button.configure(command=submit)
    root.bind("<Return>", submit)
    root.protocol("WM_DELETE_WINDOW", root.destroy)
    refresh_captcha()
    username_entry.focus_set()
    root.mainloop()
    try:
        root.destroy()
    except Exception:
        pass
    password_var.set("")
    return authenticated[0]


class Monitor:
    def __init__(self, config_path: Path, config: Mapping[str, Any]) -> None:
        self.config = config
        self.snapshot_file = resolve_path(config_path, str(config["snapshot_file"]))
        self.change_log_file = resolve_path(config_path, str(config["change_log_file"]))

    def record(self, accounts: Mapping[str, Account]) -> List[Dict[str, Any]]:
        observed_at = now_iso()
        current = {key: asdict(account) for key, account in accounts.items()}
        previous_payload = load_snapshot(self.snapshot_file)
        previous = previous_payload["accounts"] if previous_payload else None
        changes = diff_snapshots(previous, current) if previous is not None else []
        append_changes(self.change_log_file, changes, observed_at)
        atomic_write_json(
            self.snapshot_file,
            {"version": 1, "observed_at": observed_at, "target_url": self.config["target_url"], "accounts": current},
        )
        if previous is None:
            LOGGER.info("已建立初始快照（%d 个账号），首次运行不产生变更事件", len(current))
        elif not changes:
            LOGGER.info("没有变化（%d 个账号）", len(current))
        else:
            for change in changes:
                LOGGER.warning("变更：%s", human_change(change))
        return changes


def authenticate(config: MutableMapping[str, Any]) -> Optional[CocofaHttpClient]:
    client = CocofaHttpClient(config)
    try:
        if not authenticate_with_local_gui(client, config):
            client.close()
            return None
        return client
    except Exception:
        client.close()
        raise


def run_once(config_path: Path, config: MutableMapping[str, Any]) -> int:
    client = authenticate(config)
    if client is None:
        return 130
    try:
        accounts = client.fetch_accounts()
        LOGGER.info("读取到 %d 个账号", len(accounts))
        Monitor(config_path, config).record(accounts)
        return 0
    finally:
        client.close()


def run_watch(config_path: Path, config: MutableMapping[str, Any]) -> int:
    client = authenticate(config)
    if client is None:
        return 130
    stopping = False

    def stop(_signum: int, _frame: Any) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    monitor = Monitor(config_path, config)
    interval = int(config["poll_interval_seconds"])
    LOGGER.info("后台监控已启动，轮询间隔 %d 秒；关闭终端会结束内存登录会话", interval)
    try:
        while not stopping:
            try:
                accounts = client.fetch_accounts()
                LOGGER.info("读取到 %d 个账号", len(accounts))
                monitor.record(accounts)
            except AuthenticationExpired as exc:
                LOGGER.error("%s", exc)
                return 2
            except Exception:
                LOGGER.exception("本轮只读请求失败；保留上一轮快照，下个周期再试")
            # Always wait after completion. Even a full request timeout therefore
            # cannot turn into a tight retry loop.
            remaining = float(interval)
            while remaining > 0 and not stopping:
                chunk = min(1.0, remaining)
                time.sleep(chunk)
                remaining -= chunk
        LOGGER.info("监控已停止")
        return 0
    finally:
        client.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Cocofa 账号管理后台只读监控")
    parser.add_argument(
        "command",
        nargs="?",
        default="watch",
        choices=("once", "watch"),
        help="登录后检查一次，或持续后台监控（默认 watch）",
    )
    default_config = Path(sys.executable).resolve().parent / "config.json" if getattr(sys, "frozen", False) else Path("config.json")
    parser.add_argument("--config", default=str(default_config), help="配置文件路径（默认使用程序旁的 config.json）")
    return parser


def make_console_unicode_safe() -> None:
    """Do not crash when Windows uses a legacy non-Chinese console code page."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, OSError):
            pass


def main(argv: Optional[Sequence[str]] = None) -> int:
    make_console_unicode_safe()
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config_path = Path(args.config).resolve()
    try:
        config = load_config(config_path)
        if args.command == "once":
            return run_once(config_path, config)
        return run_watch(config_path, config)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        LOGGER.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
