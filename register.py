#!/usr/bin/env python3
"""Account auto-registration for elevenlabs-stt.

temp-mail (cloudflare_temp_email) + real Chrome UI automation (pyautogui) —
the Windows-only, most platform-specific corner of the tool, kept out of
stt.py's API/packing/pipeline core. stt.py imports this lazily at its call
sites (refill_pool / run_plan_pipelined / cmd_pool_warm) to avoid an import
cycle; this module only touches stt.* at call time.
"""
from __future__ import annotations

import html
import json
import os
import pathlib
import re
import secrets
import shutil
import subprocess
import tempfile
import time
from typing import Any

import httpx

import stt


# --- temp-email --------------------------------------------------------

def temp_email_create(name: str | None = None,
                      cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Create a cloudflare_temp_email address; admin path first, user path fallback."""
    cfg = cfg or stt.temp_email_config()
    if not cfg["base_url"] or not cfg["domain"]:
        raise SystemExit("temp_email.base_url and temp_email.domain are required")
    base = str(cfg["base_url"]).rstrip("/")
    # cloudflare_temp_email v1.9 requires name even on the admin API.
    local = name or ("el" + secrets.token_hex(5))
    body = {"name": local, "domain": cfg["domain"], "cf_token": "",
            "enableRandomSubdomain": False}

    with httpx.Client(timeout=30) as client:
        if cfg.get("use_admin_path", True) and cfg.get("admin_password"):
            r = client.post(f"{base}/admin/new_address", json=body,
                            headers={"x-admin-auth": cfg["admin_password"]})
            if r.status_code < 400:
                return r.json()
            if r.status_code not in (401, 403):
                raise SystemExit(f"temp-email create failed ({r.status_code}): {r.text[:300]}")

        headers = {}
        if cfg.get("site_password"):
            headers["x-custom-auth"] = cfg["site_password"]
        r = client.post(f"{base}/api/new_address", json=body, headers=headers)
        if r.status_code >= 400:
            raise SystemExit(f"temp-email create failed ({r.status_code}): {r.text[:300]}")
        return r.json()


def latest_verify_link(addr_jwt: str, cfg: dict[str, Any] | None = None) -> str:
    """Return newest ElevenLabs verification link from a temp mailbox."""
    cfg = cfg or stt.temp_email_config()
    base = str(cfg["base_url"]).rstrip("/")
    deadline = time.time() + float(cfg["poll_timeout_secs"])
    headers = {"Authorization": f"Bearer {addr_jwt}"}
    with httpx.Client(timeout=30) as client:
        while time.time() < deadline:
            r = client.get(f"{base}/api/parsed_mails", params={"limit": 20, "offset": 0},
                           headers=headers)
            if r.status_code >= 400:
                raise SystemExit(f"temp-email poll failed ({r.status_code}): {r.text[:300]}")
            for mail in r.json().get("results", []):
                text = html.unescape("\n".join(str(mail.get(k) or "") for k in ("text", "html")))
                match = re.search(r"https://elevenlabs\.io/app/action\?[^\s\"<>]+oobCode=[^\s\"<>]+", text)
                if match:
                    return match.group(0)
            time.sleep(float(cfg["poll_interval_secs"]))
    raise SystemExit("timed out waiting for ElevenLabs verification email")


# --- register ----------------------------------------------------------

def register_one() -> dict[str, Any]:
    """Create one ElevenLabs account via temp-mail + real Chrome, then return account."""
    try:
        import pyautogui, pyperclip, pygetwindow as gw
    except ImportError:
        raise SystemExit("auto-register needs pyautogui pyperclip pygetwindow")

    stt._rlog("创建临时邮箱...")
    addr = temp_email_create()
    email = addr["address"]
    stt._rlog(f"临时邮箱已创建: {email}")
    password = stt.random_password()
    chrome = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    profile_dir = tempfile.mkdtemp(prefix="elevenlabs-stt-chrome-")
    prefs_path = pathlib.Path(profile_dir) / "Default" / "Preferences"
    prefs_path.parent.mkdir(parents=True, exist_ok=True)
    prefs_path.write_text(json.dumps({
        "credentials_enable_service": False,
        "profile": {"password_manager_enabled": False},
    }), encoding="utf-8")

    # ponytail: fresh profile per account avoids logged-in Chrome redirecting sign-up to onboarding.
    # Coordinates are ugly, but selector automation triggers hCaptcha; real Chrome doesn't.
    def profile_window_handles() -> set[int]:
        if not shutil.which("powershell"):
            return set()
        profile_name = pathlib.Path(profile_dir).name.replace("'", "''")
        ps = (
            f"$profile = '{profile_name}'; "
            "$pids = @(Get-CimInstance Win32_Process -Filter \"name='chrome.exe'\" | "
            "Where-Object { $_.CommandLine -and $_.CommandLine.Contains($profile) } | "
            "Select-Object -ExpandProperty ProcessId); "
            "if ($pids.Count -gt 0) { "
            "Get-Process -Id $pids -ErrorAction SilentlyContinue | "
            "Where-Object { $_.MainWindowHandle -ne 0 } | "
            "ForEach-Object { $_.MainWindowHandle } "
            "}"
        )
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                                 capture_output=True, text=True, timeout=3)
        except Exception:
            return set()
        handles: set[int] = set()
        for line in out.stdout.splitlines():
            try:
                handles.add(int(line.strip()))
            except ValueError:
                pass
        return handles

    chrome_startup_kwargs = {}
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 1  # SW_SHOWNORMAL: do not inherit a hidden Web UI process state.
        chrome_startup_kwargs = {
            "startupinfo": startupinfo,
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP,
        }
    proc = None
    try:
        stt._rlog("启动临时 Chrome...")
        proc = subprocess.Popen([
            chrome,
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--new-window",
            "--window-position=40,40",
            "--disable-save-password-bubble",
            "--do-not-de-elevate",
            "https://elevenlabs.io/app/sign-up",
        ], **chrome_startup_kwargs)
        new_window = None
        stt._rlog("等待临时 Chrome 窗口出现（最长 30s）...")
        # 30s: a brand-new profile cold-starts slowly (profile init + AV scan); 10s
        # missed the window on busy machines and the finally-block killed late Chrome.
        deadline = time.time() + 30
        next_profile_probe = 0.0
        while time.time() < deadline and new_window is None:
            time.sleep(0.5)
            profile_handles = set()
            if time.time() >= next_profile_probe:
                profile_handles = profile_window_handles()
                next_profile_probe = time.time() + 1.0
            for w in gw.getAllWindows():
                hwnd = getattr(w, "_hWnd", None)
                if hwnd in profile_handles:
                    new_window = w
                    break
        if new_window is None:
            raise SystemExit("auto-register could not find the new temporary Chrome window; aborting")

        def window_op(name: str) -> None:
            try:
                getattr(new_window, name)()
            except Exception as e:
                # pygetwindow/pywin32 can report Windows error code 0 ("success")
                # after the window operation actually completed. Treat only that
                # wrapper bug as non-fatal; real focus/window errors should abort.
                if "Error code from Windows: 0" not in str(e):
                    raise

        def ensure_window_foreground() -> None:
            hwnd = getattr(new_window, "_hWnd", None)
            if not hwnd or os.name != "nt":
                window_op("activate")
                return
            import ctypes
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            # Fast path: already foreground. Forcing anyway is what caused the
            # constant restore/maximize flicker between every automation action.
            if user32.GetForegroundWindow() == hwnd:
                return
            SW_RESTORE = 9
            HWND_TOPMOST = -1
            HWND_NOTOPMOST = -2
            SWP_NOSIZE = 0x0001
            SWP_NOMOVE = 0x0002
            SWP_SHOWWINDOW = 0x0040
            flags = SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW
            for attempt in range(8):
                if user32.IsIconic(hwnd):
                    user32.ShowWindow(hwnd, SW_RESTORE)
                # AttachThreadInput to the current foreground thread satisfies
                # Windows' foreground-lock rules. A synthetic Alt tap also works
                # but toggles Chrome's menu-accelerator mode, breaking in-window
                # keyboard focus for the very keys we send next.
                fg = user32.GetForegroundWindow()
                fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
                cur_tid = kernel32.GetCurrentThreadId()
                attached = fg_tid and fg_tid != cur_tid and user32.AttachThreadInput(cur_tid, fg_tid, True)
                try:
                    user32.BringWindowToTop(hwnd)
                    user32.SetForegroundWindow(hwnd)
                    if attempt >= 4:  # last resort: topmost toggle
                        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, flags)
                        user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, flags)
                finally:
                    if attached:
                        user32.AttachThreadInput(cur_tid, fg_tid, False)
                time.sleep(0.2)
                if user32.GetForegroundWindow() == hwnd:
                    return
            raise SystemExit("auto-register could not focus the temporary Chrome window; aborting before sending keys")

        # Foreground the temp Chrome immediately, before any page-load waits.
        # pygetwindow's activate() is silently denied when we are a background
        # process, which left the window behind for ~16s until the first click
        # forced it forward and the key/click sequence landed out of sync.
        stt._rlog("窗口已找到，置顶并等待页面渲染...")
        window_op("restore")
        window_op("maximize")
        ensure_window_foreground()
        time.sleep(4)

        def hotkey(*keys: str) -> None:
            ensure_window_foreground()
            pyautogui.hotkey(*keys)

        def press(key: str) -> None:
            ensure_window_foreground()
            pyautogui.press(key)

        def click_frac(x_frac: float, y_frac: float) -> None:
            ensure_window_foreground()
            x = new_window.left + int(new_window.width * x_frac)
            y = new_window.top + int(new_window.height * y_frac)
            if x < 0 or y < 0:
                # A minimized window reports -32000 geometry; pyautogui clamps the
                # click to (0,0), which hits Chrome's tab-search chevron.
                raise SystemExit("auto-register got bad temp Chrome window geometry; aborting")
            pyautogui.click(x, y)
            time.sleep(0.1)

        def paste(text: str) -> None:
            ensure_window_foreground()
            pyperclip.copy(text)
            pyautogui.hotkey("ctrl", "v")

        # Chrome already opened /app/sign-up from its command line; just wait
        # for the app to render instead of re-navigating (visible reload).
        time.sleep(12)

        stt._rlog("填写注册表单...")
        click_frac(0.50, 0.56)  # signup email
        hotkey("ctrl", "a"); paste(email)
        press("tab"); paste(password)
        press("enter")

        stt._rlog(f"等待验证邮件（最长 {stt.temp_email_config()['poll_timeout_secs']}s）...")
        link = latest_verify_link(addr["jwt"])
        stt._rlog("打开验证链接并确认...")
        hotkey("ctrl", "l")
        paste(link)
        press("enter")
        time.sleep(15)
        press("enter")  # modal Continue if focused
        click_frac(0.50, 0.62)
        click_frac(0.65, 0.62)  # verification modal Continue fallback
        time.sleep(8)
        stt._rlog("用新账号登录...")
        click_frac(0.50, 0.62)  # sign-in email
        hotkey("ctrl", "a"); paste(email)
        press("tab"); paste(password)
        press("enter")
        time.sleep(15)

        stt._rlog("拉取账号积分...")
        account = stt.account_from_password_signin(email, password, temp_address=email)
        with stt.authed_client(account, save=lambda _s: None) as client:
            client.get("/v1/user")
            stt.refresh_credits(account, client)
        stt._rlog(f"注册完成: {email}，剩余积分 {stt.cached_remaining(account)}")
        return account
    finally:
        if shutil.which("powershell"):
            profile_name = pathlib.Path(profile_dir).name.replace("'", "''")
            subprocess.run([
                "powershell", "-NoProfile", "-Command",
                f"$profile = '{profile_name}'; "
                "Get-CimInstance Win32_Process -Filter \"name='chrome.exe'\" | "
                "Where-Object { $_.CommandLine -and $_.CommandLine.Contains($profile) } | "
                "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }",
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif proc is not None:
            proc.terminate()
        for _ in range(5):
            shutil.rmtree(profile_dir, ignore_errors=True)
            if not pathlib.Path(profile_dir).exists():
                break
            time.sleep(0.5)
