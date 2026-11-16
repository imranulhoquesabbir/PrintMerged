import os, sys, json, time, threading, urllib.request
if sys.stdout is None: sys.stdout = open(os.devnull, "w")
if sys.stderr is None: sys.stderr = open(os.devnull, "w")
import logging
logging.getLogger("werkzeug").setLevel(logging.ERROR)

# NOTE: server (flask, fitz, PIL, printer libraries) and updatemgr are imported inside main(), AFTER we know that this
# is the first window. Every later start (Image Fixer Ctrl+P, 'Open with', 'Send to') only hands its files to the open window
# and quits: before, it loaded all the heavy libraries first, which made the PC hang and the window appear late.
make_server = server = updatemgr = None
CFG_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "PCPrint")   # same folder as server.CFG_DIR

MUTEX = None   # keeps the "one window only" lock alive
EXTS = (".pdf", ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff")
win = None
tray = None
QUIT = {"v": False}


def tray_image():
    """Small icon for the system tray."""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, 63, 63), radius=14, fill=(37, 99, 235))
    d.rectangle((20, 10, 44, 26), fill="white")
    d.rounded_rectangle((12, 24, 52, 46), radius=4, fill="white")
    d.rectangle((20, 38, 44, 54), fill="white", outline=(37, 99, 235), width=2)
    d.rectangle((24, 43, 40, 45), fill=(37, 99, 235))
    return img


def native_front():
    """Windows does not let a program that runs in the background take the front: the window only blinks in the task bar.
    The tap on ALT lifts that rule for this moment (same trick as printing.py).
    Runs in a Flask thread, so it must never WAIT for the window's own (UI) thread: before, GetWindowTextW /
    ShowWindow / BringWindowToTop on our own window sent messages to the UI thread and blocked until it answered
    (a hang when the UI thread was busy).  Now: FindWindowW (no message), ShowWindowAsync (posts, does not wait).
    Returns True when the window was found."""
    try:
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        u.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        u.FindWindowW.restype = wintypes.HWND
        u.IsIconic.argtypes = [wintypes.HWND]
        u.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
        u.SetForegroundWindow.argtypes = [wintypes.HWND]
        u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        hwnd = u.FindWindowW(None, "PC Print")
        if not hwnd:
            return False
        pid = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value != os.getpid():          # another program has a window with the same title
            return False
        u.ShowWindowAsync(hwnd, 9 if u.IsIconic(hwnd) else 5)     # SW_RESTORE / SW_SHOW (hidden in the tray too)
        u.keybd_event(0x12, 0, 0, 0)                              # ALT down / up
        u.keybd_event(0x12, 0, 2, 0)
        u.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


def front():
    """Show the window (also when it is hidden in the tray) and bring it to the front."""
    if native_front():
        return
    try:                                       # fallback only when the native window was not found
        win.show()
        win.restore()
    except Exception:
        pass


def quit_app(*a):
    QUIT["v"] = True
    try:
        win.destroy()
    except Exception:
        pass


def notify(title, text):
    """Windows tray message (used by auto update). Does nothing when there is no tray icon."""
    try:
        if tray:
            tray.notify(text, title)
    except Exception:
        pass


def start_tray():
    """System tray icon: click = open, menu = Open / Quit. Returns True if it worked."""
    global tray
    try:
        import pystray
        import pystray._win32  # noqa: makes PyInstaller bundle the Windows backend
        menu = pystray.Menu(pystray.MenuItem("Open PC Print", lambda icon, item: front(), default=True),
                            pystray.MenuItem("Quit", lambda icon, item: quit_app()))
        tray = pystray.Icon("PCPrint", tray_image(), "PC Print - Imranul Hoque Sabbir", menu)
        tray.run_detached()
        return True
    except Exception:
        tray = None
        return False


def argv_paths():
    return [os.path.abspath(a) for a in sys.argv[1:] if not a.startswith("--") and os.path.isfile(a)]


def port_file():
    return os.path.join(CFG_DIR, "port.txt")


def is_primary():
    """True for the first launch. Later launches (Windows starts one process per file)
    hand their files to the first window. Uses a Windows mutex, which is reliable
    (two programs can share a TCP port on Windows, so the port is not used for this)."""
    global MUTEX
    try:
        import ctypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateMutexW.restype = ctypes.c_void_p
        MUTEX = k.CreateMutexW(None, False, "Local\\PCPrintSingleWindow")
        return ctypes.get_last_error() != 183   # 183 = already exists
    except Exception:
        return True


def ping(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=1) as r:
            return json.load(r).get("app") == "pcprint"
    except Exception:
        return False


def allow_foreground():
    """This (new) process was just started by the user's click, so Windows lets it hand the front to the open window."""
    try:
        import ctypes
        ctypes.windll.user32.AllowSetForegroundWindow(-1)        # ASFW_ANY
    except Exception:
        pass


def forward(paths):
    """Send files to the window that is already open (waits until it is ready)."""
    allow_foreground()
    for _ in range(60):
        try:
            with open(port_file()) as fh:
                port = int(fh.read().strip())
            if ping(port):
                req = urllib.request.Request(f"http://127.0.0.1:{port}/api/add",
                                             data=json.dumps({"paths": paths}).encode(),
                                             headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=5).read()
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "PCPrint"


def autostart_get():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, RUN_NAME)
            return True
    except Exception:
        return False


def autostart_set(on):
    """Start with Windows (current user only, no admin needed). Launched with --startup."""
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
        if on:
            winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, f'"{sys.executable}" --startup')
        else:
            try:
                winreg.DeleteValue(k, RUN_NAME)
            except FileNotFoundError:
                pass


def make_send_to():
    """'Send to > PC Print' for many files at once (Windows then starts the app one time)."""
    try:
        folder = os.path.join(os.environ["APPDATA"], "Microsoft", "Windows", "SendTo")
        lnk = os.path.join(folder, "PC Print.lnk")
        if os.path.exists(lnk):
            try:   # the exe was moved? then make the shortcut again
                import win32com.client
                import pythoncom
                pythoncom.CoInitialize()
                try:
                    cur = win32com.client.Dispatch("WScript.Shell").CreateShortCut(lnk).TargetPath
                finally:
                    pythoncom.CoUninitialize()
                if os.path.normcase(cur) == os.path.normcase(sys.executable):
                    return
            except Exception:
                return
        import win32com.client
        import pythoncom
        pythoncom.CoInitialize()
        try:
            sc = win32com.client.Dispatch("WScript.Shell").CreateShortCut(lnk)
            sc.TargetPath = sys.executable
            sc.WorkingDirectory = os.path.dirname(sys.executable)
            sc.save()
        finally:
            pythoncom.CoUninitialize()
    except Exception:
        pass


def register_open_with():
    """Adds 'PC Print' to the right-click > Open with menu (current user only, no admin)."""
    import winreg
    exe = sys.executable
    name = os.path.basename(exe)
    cmd = f'"{exe}" "%1"'

    def open_with_ready():
        try:
            base = r"Software\Classes\Applications" + "\\" + name
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, base + r"\shell\open\command") as k:
                if winreg.QueryValueEx(k, "")[0] != cmd:
                    return False
            for ext in EXTS:
                winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                               r"Software\Classes" + "\\" + ext + r"\OpenWithList" + "\\" + name))
            return True
        except OSError:
            return False

    if open_with_ready():
        return          # everything is there already: do not touch the registry or bother Explorer on every start

    def setv(path, value_name, value):
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, path) as k:
            winreg.SetValueEx(k, value_name, 0, winreg.REG_SZ, value)

    base = r"Software\Classes\Applications" + "\\" + name
    setv(base, "FriendlyAppName", "PC Print")
    setv(base + r"\shell\open\command", "", f'"{exe}" "%1"')
    for ext in EXTS:
        setv(base + r"\SupportedTypes", ext, "")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                              r"Software\Classes" + "\\" + ext + r"\OpenWithList" + "\\" + name):
            pass
    try:
        import ctypes
        # SHCNF_FLUSHNOWAIT: do not wait for Explorer (waiting for it could freeze the start for seconds)
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0x3000, None, None)
    except Exception:
        pass


def unregister_open_with():
    import winreg
    name = os.path.basename(sys.executable)

    def kill(path):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as k:
                subs = [winreg.EnumKey(k, i) for i in range(winreg.QueryInfoKey(k)[0])]
            for sub in subs:
                kill(path + "\\" + sub)
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
        except Exception:
            pass

    try:
        os.remove(os.path.join(os.environ["APPDATA"], "Microsoft", "Windows", "SendTo", "PC Print.lnk"))
    except Exception:
        pass
    try:
        autostart_set(False)
    except Exception:
        pass
    kill(r"Software\Classes\Applications" + "\\" + name)
    for ext in EXTS:
        kill(r"Software\Classes" + "\\" + ext + r"\OpenWithList" + "\\" + name)


SRV = {"s": None}


def bind(port):
    """Listen on the LAN (phones) + this PC. If the port is busy, fall back to this PC only."""
    old = SRV["s"]
    if old:
        try:
            old.shutdown()
            old.server_close()
        except Exception:
            pass
    try:
        s = make_server("0.0.0.0", port, server.app, threaded=True)
        server.PH["ok"] = True
    except OSError:
        s = make_server("127.0.0.1", 0, server.app, threaded=True)
        server.PH["ok"] = False
    SRV["s"] = s
    server.PH["port"] = s.server_port
    threading.Thread(target=s.serve_forever, daemon=True).start()
    try:
        os.makedirs(server.CFG_DIR, exist_ok=True)
        with open(port_file(), "w") as fh:
            fh.write(str(s.server_port))
    except Exception:
        pass
    return s.server_port


def main():
    if "--unregister" in sys.argv:
        unregister_open_with()
        return
    paths = argv_paths()

    startup = "--startup" in sys.argv   # started by Windows: open minimized
    if not is_primary():
        if not startup:
            forward(paths)
        return

    global make_server, server, updatemgr
    from werkzeug.serving import make_server
    import server
    import updatemgr

    port = bind(server.ph_cfg()["port"])
    server.HOOKS["rebind"] = bind

    if getattr(sys, "frozen", False):
        # registry + 'Send to' shortcut are not needed to show the window: do them in the background
        def shell_setup():
            try:
                register_open_with()
            except Exception:
                pass
            make_send_to()
        threading.Thread(target=shell_setup, daemon=True).start()
    if paths:
        server.register(paths, sort=True)

    import webview
    global win
    tray_ok = start_tray()
    win = webview.create_window("PC Print", f"http://127.0.0.1:{port}/", width=1120, height=820,
                                min_size=(480, 400),
                                hidden=startup and tray_ok,        # started by Windows: only the tray icon
                                minimized=startup and not tray_ok)

    def on_closing(*a):
        # with "Start with Windows" on, the X button hides the window to the tray
        if not QUIT["v"] and tray and autostart_get():
            win.hide()
            return False
        return True

    win.events.closing += on_closing

    def pick():
        kind = webview.FileDialog.OPEN if hasattr(webview, "FileDialog") else webview.OPEN_DIALOG
        r = win.create_file_dialog(
            kind, allow_multiple=True,
            file_types=("PDF and images (*.pdf;*.jpg;*.jpeg;*.png;*.webp;*.bmp;*.gif;*.tif;*.tiff)",))
        return list(r or [])

    server.HOOKS["pick"] = pick

    def pickdir():
        kind = webview.FileDialog.FOLDER if hasattr(webview, "FileDialog") else webview.FOLDER_DIALOG
        r = win.create_file_dialog(kind)
        return str(list(r)[0]) if r else ""

    server.HOOKS["pickdir"] = pickdir
    updatemgr.HOOKS["quit"] = quit_app
    updatemgr.HOOKS["notify"] = notify
    server.HOOKS["front"] = front
    if getattr(sys, "frozen", False):
        server.HOOKS["autostart_get"] = autostart_get
        server.HOOKS["autostart_set"] = autostart_set
    updatemgr.start()      # auto update: looks for a new build a few seconds after start (if switched on)
    try:
        # same profile every start: the window opens faster than with a new temporary one each time
        webview.start(private_mode=False, storage_path=os.path.join(server.CFG_DIR, "webview"))
    except TypeError:      # an old pywebview without these options
        webview.start()
    if tray:
        try:
            tray.stop()
        except Exception:
            pass
    try:
        SRV["s"].shutdown()
    except Exception:
        pass
    server.cleanup()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, str(e), "PC Print", 0x10)
        except Exception:
            pass
